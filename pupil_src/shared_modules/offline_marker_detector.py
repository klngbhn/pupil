'''
(*)~----------------------------------------------------------------------------------
 Pupil - eye tracking platform
 Copyright (C) 2012-2014  Pupil Labs

 Distributed under the terms of the CC BY-NC-SA License.
 License details are in the file license.txt, distributed as part of this software.
----------------------------------------------------------------------------------~(*)
'''

import sys, os,platform
import cv2
import numpy as np
import shelve


if platform.system() == 'Darwin':
    from billiard import Process,Queue,forking_enable
    from billiard.sharedctypes import Value
else:
    from multiprocessing import Process, Pipe, Event, Queue
    forking_enable = lambda x: x #dummy fn
    from multiprocessing.sharedctypes import Value



from gl_utils import draw_gl_polyline,adjust_gl_view,draw_gl_polyline_norm,clear_gl_screen,draw_gl_point,draw_gl_points,draw_gl_point_norm,draw_gl_points_norm,basic_gl_setup,cvmat_to_glmat, draw_named_texture
from OpenGL.GL import *
from OpenGL.GLU import gluOrtho2D
from methods import normalize,denormalize,Cache_List
from glfw import *
import atb
from ctypes import c_int,c_bool,create_string_buffer

from plugin import Plugin
#logging
import logging
logger = logging.getLogger(__name__)

from square_marker_detect import detect_markers_robust,detect_markers_simple, draw_markers,m_marker_to_screen
from reference_surface import Reference_Surface
from math import sqrt


class Offline_Marker_Detector(Plugin):
    """docstring

    """
    def __init__(self,g_pool,atb_pos=(320,220)):
        super(Offline_Marker_Detector, self).__init__()
        self.g_pool = g_pool
        self.order = .2

        # all markers that are detected in the most recent frame
        self.markers = []
        # all registered surfaces

        if g_pool.app == 'capture':
           raise Exception('For Player only.')
        #in player we load from the rec_dir: but we have a couple options:
        self.surface_definitions = shelve.open(os.path.join(g_pool.rec_dir,'surface_definitions'),protocol=2)
        if self.load('offline_square_marker_surfaces',[]) != []:
            logger.debug("Found ref surfaces defined or copied in previous session.")
            self.surfaces = [Reference_Surface(saved_definition=d) for d in self.load('offline_square_marker_surfaces',[]) if isinstance(d,dict)]
        elif self.load('offline_square_marker_surfaces',[]) != []:
            logger.debug("Did not find ref surfaces def created or used by the user in player from earlier session. Loading surfaces defined during capture.")
            self.surfaces = [Reference_Surface(saved_definition=d) for d in self.load('realtime_square_marker_surfaces',[]) if isinstance(d,dict)]
        else:
            logger.debug("No surface defs found. Please define using GUI.")
            self.surfaces = []

        # edit surfaces
        self.surface_edit_mode = c_bool(0)
        self.edit_surfaces = []

        #detector vars
        self.robust_detection = c_bool(1)
        self.aperture = c_int(11)
        self.min_marker_perimeter = 80

      
        #check if marker cache is available from last session
        self.persistent_cache = shelve.open(os.path.join(g_pool.rec_dir,'square_marker_cache'),protocol=2)
        self.cache = Cache_List(self.persistent_cache.get('marker_cache',[False for _ in g_pool.timestamps]))
        logger.debug("Loaded marker cache %s / %s frames had been searched before"%(len(self.cache)-self.cache.count(False),len(self.cache)) )
        self.init_marker_cacher()

        #debug vars
        self.draw_markers = c_bool(0)
        self.show_surface_idx = c_int(0)
        self.recent_pupil_positions = []

        self.img_shape = None

        atb_label = "marker detection"
        self._bar = atb.Bar(name =self.__class__.__name__, label=atb_label,
            help="marker detection parameters", color=(50, 50, 50), alpha=100,
            text='light', position=atb_pos,refresh=.3, size=(300, 80))
        self._bar.add_var("draw markers",self.draw_markers)
        self._bar.add_button('close',self.unset_alive)


        atb_pos = atb_pos[0],atb_pos[1]+90
        self._bar_markers = atb.Bar(name =self.__class__.__name__+'markers', label='registered surfaces',
            help="list of registered ref surfaces", color=(50, 100, 50), alpha=100,
            text='light', position=atb_pos,refresh=.3, size=(300, 120))
        self.update_bar_markers()



    def unset_alive(self):
        self.alive = False

    def load(self, var_name, default):
        return self.surface_definitions.get(var_name,default)
    def save(self, var_name, var):
            self.surface_definitions[var_name] = var


    def on_click(self,pos,button,action):
        if self.surface_edit_mode.value:
            if self.edit_surfaces:
                if action == GLFW_RELEASE:
                    self.edit_surfaces = []
            # no surfaces verts in edit mode, lets see if the curser is close to one:
            else:
                if action == GLFW_PRESS:
                    surf_verts = ((0.,0.),(1.,0.),(1.,1.),(0.,1.))
                    x,y = pos
                    for s in self.surfaces:
                        if s.detected:
                            for (vx,vy),i in zip(s.ref_surface_to_img(np.array(surf_verts)),range(4)):
                                vx,vy = denormalize((vx,vy),(self.img_shape[1],self.img_shape[0]),flip_y=True)
                                if sqrt((x-vx)**2 + (y-vy)**2) <15: #img pixels
                                    self.edit_surfaces.append((s,i))

    def advance(self):
        pass

    def add_surface(self):
        self.surfaces.append(Reference_Surface())
        self.update_bar_markers()

    def remove_surface(self,i):
        self.surfaces[i].cleanup()
        del self.surfaces[i]
        self.update_bar_markers()


    def update_bar_markers(self):
        self._bar_markers.clear()
        self._bar_markers.add_button("  add surface   ", self.add_surface, key='a')
        self._bar_markers.add_var("  edit mode   ", self.surface_edit_mode )
        for s,i in zip(self.surfaces,range(len(self.surfaces)))[::-1]:
            self._bar_markers.add_var("%s_window"%i,setter=s.toggle_window,getter=s.window_open,group=str(i),label='open in window')
            self._bar_markers.add_var("%s_name"%i,create_string_buffer(512),getter=s.atb_get_name,setter=s.atb_set_name,group=str(i),label='name')
            self._bar_markers.add_var("%s_markers"%i,create_string_buffer(512), getter=s.atb_marker_status,group=str(i),label='found/registered markers' )
            self._bar_markers.add_button("%s_remove"%i, self.remove_surface,data=i,label='remove',group=str(i))

    def update(self,frame,recent_pupil_positions,events):
        self.img_shape = frame.img.shape
        self.update_marker_cache()
        self.markers = self.cache[frame.index]
        if self.markers == False:
            # locate markers because precacher has not anayzed this frame yet. Most likely a seek event
            self.markers = []
            self.seek_marker_cacher(frame.index) # tell precacher that it better have every thing from here analyzed
            return
       

        # locate surfaces
        for s in self.surfaces:
            if not s.locate_from_cache(frame.index):
                # logger.debug("location not from cache")
                s.locate(self.markers)
            if s.detected:
                events.append({'type':'marker_ref_surface','name':s.name,'uid':s.uid,'m_to_screen':s.m_to_screen,'m_from_screen':s.m_from_screen, 'timestamp':frame.timestamp})

        if self.draw_markers.value:
            draw_markers(frame.img,self.markers)

        # edit surfaces by user
        if self.surface_edit_mode:
            window = glfwGetCurrentContext()
            pos = glfwGetCursorPos(window)
            pos = normalize(pos,glfwGetWindowSize(window))
            pos = denormalize(pos,(frame.img.shape[1],frame.img.shape[0]) ) # Position in img pixels

            for s,v_idx in self.edit_surfaces:
                if s.detected:
                    pos = normalize(pos,(self.img_shape[1],self.img_shape[0]),flip_y=True)
                    new_pos =  s.img_to_ref_surface(np.array(pos))
                    s.move_vertex(v_idx,new_pos)
        else:
            # update srf with no or invald cache:
            for s in self.surfaces:
                if s.cache == None:
                    s.init_cache(self.cache)

        #map recent gaze onto detected surfaces used for pupil server
        for s in self.surfaces:
            if s.detected:
                s.recent_gaze = []
                for p in recent_pupil_positions:
                    if p['norm_pupil'] is not None:
                        gp_on_s = tuple(s.img_to_ref_surface(np.array(p['norm_gaze'])))
                        p['realtime gaze on '+s.name] = gp_on_s
                        s.recent_gaze.append(gp_on_s)


        #allow surfaces to open/close windows
        for s in self.surfaces:
            if s.window_should_close:
                s.close_window()
            if s.window_should_open:
                s.open_window()

    def init_marker_cacher(self):
        forking_enable(0) #for MacOs only
        from marker_detector_cacher import fill_cache
        visited_list = [False if x == False else True for x in self.cache]
        video_file_path =  os.path.join(self.g_pool.rec_dir,'world.avi')
        self.cache_queue = Queue()
        self.cacher_seek_idx = Value(c_int,0)
        self.cacher_run = Value(c_bool,True)
        self.cacher = Process(target=fill_cache, args=(visited_list,video_file_path,self.cache_queue,self.cacher_seek_idx,self.cacher_run))
        self.cacher.start()

    def update_marker_cache(self):
        while not self.cache_queue.empty():
            idx,c_m = self.cache_queue.get()
            self.cache.update(idx,c_m)
            for s in self.surfaces:
                s.update_cache(self.cache,idx=idx)

    def seek_marker_cacher(self,idx):
        self.cacher_seek_idx.value = idx

    def close_marker_cacher(self):
        self.update_marker_cache()
        self.cacher_run.value = False
        self.cacher.join()

    def gl_display(self):
        """
        Display marker and surface info inside world screen
        """
        self.gl_display_cache_bars()

        for m in self.markers:
            hat = np.array([[[0,0],[0,1],[.5,1.3],[1,1],[1,0],[0,0]]],dtype=np.float32)
            hat = cv2.perspectiveTransform(hat,m_marker_to_screen(m))
            draw_gl_polyline(hat.reshape((6,2)),(0.1,1.,1.,.5))

        for s in self.surfaces:
            s.gl_draw_frame()
            s.gl_display_in_window(self.g_pool.image_tex)

        if self.surface_edit_mode.value:
            for s in  self.surfaces:
                s.gl_draw_corners()


    def gl_display_cache_bars(self):
        """
        """
        padding = 20.

       # Lines for areas that have be cached
        cached_ranges = []
        for r in self.cache.ranges: # [[0,1],[3,4]]
            cached_ranges += (r[0],0),(r[1],0) #[(0,0),(1,0),(3,0),(4,0)]

        # Lines where surfaces have been found in video
        cached_surfaces = []
        for s in self.surfaces:
            found_at = []
            if s.cache is not None:
                for r in s.cache.ranges: # [[0,1],[3,4]]
                    found_at += (r[0],0),(r[1],0) #[(0,0),(1,0),(3,0),(4,0)]
                cached_surfaces.append(found_at)

        glMatrixMode(GL_PROJECTION)
        glPushMatrix()
        glLoadIdentity()
        width,height = glfwGetWindowSize(glfwGetCurrentContext())
        h_pad = padding/width * self.cache.length
        v_pad = padding/height
        gluOrtho2D(-h_pad, self.cache.length+h_pad, -v_pad, 1+v_pad) # ranging from 0 to cache_len (horizontal) and 0 to 1 (vertical)
        glMatrixMode(GL_MODELVIEW)
        glPushMatrix()
        glLoadIdentity()

        color = (1.,.4,.5,8.)
        draw_gl_polyline(cached_ranges,color=color,type='Lines')

        for s in cached_surfaces:
            glTranslatef(0,.02,0)
            draw_gl_polyline(s,color=color,type='Lines')

        glMatrixMode(GL_PROJECTION)
        glPopMatrix()
        glMatrixMode(GL_MODELVIEW)
        glPopMatrix()





    def cleanup(self):
        """ called when the plugin gets terminated.
        This happends either voluntary or forced.
        if you have an atb bar or glfw window destroy it here.
        """
 
        self.save("offline_square_marker_surfaces",[rs.save_to_dict() for rs in self.surfaces if rs.defined])
        self.close_marker_cacher()
        self.persistent_cache["marker_cache"] = self.cache.to_list()
        self.persistent_cache.close()

        self.surface_definitions.close()

        for s in self.surfaces:
            s.close_window()
        self._bar.destroy()
        self._bar_markers.destroy()


