# Copyright (c) 2022 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""Command line interface integrating options to record maps with WASD controls. """
import argparse
import logging
import os
import sys
import time
import cv2
import numpy as np
import tkinter as tk
from tkinter import simpledialog

import google.protobuf.timestamp_pb2
import graph_nav_util
import grpc
from google.protobuf import wrappers_pb2 as wrappers

import bosdyn.client.channel
import bosdyn.client.util
from bosdyn.api.graph_nav import map_pb2, map_processing_pb2, recording_pb2
from bosdyn.client import ResponseError, RpcError, create_standard_sdk
from bosdyn.client.graph_nav import GraphNavClient
from bosdyn.client.map_processing import MapProcessingServiceClient
from bosdyn.client.math_helpers import Quat, SE3Pose
from bosdyn.client.recording import GraphNavRecordingServiceClient
from bosdyn.client.image import ImageClient
from bosdyn.api import geometry_pb2, image_pb2,manipulation_api_pb2

# g_image_click = []
# g_image_display = []

class RecordingInterface(object):
    """Recording service command line interface."""

    def __init__(self, robot, download_filepath, client_metadata):
        # Keep the robot instance and it's ID.
        self._robot = robot
        # Force trigger timesync.
        self._robot.time_sync.wait_for_sync()

        # Filepath for the location to put the downloaded graph and snapshots.
        if download_filepath[-1] == "/":
            self._download_filepath = download_filepath + "downloaded_graph"
        else:
            self._download_filepath = download_filepath + "/downloaded_graph"

        # Setup the recording service client.
        self._recording_client = self._robot.ensure_client(
            GraphNavRecordingServiceClient.default_service_name)

        # Create the recording environment.
        self._recording_environment = GraphNavRecordingServiceClient.make_recording_environment(
            waypoint_env=GraphNavRecordingServiceClient.make_waypoint_environment(
                client_metadata=client_metadata))

        # Setup the graph nav service client.
        self._graph_nav_client = robot.ensure_client(GraphNavClient.default_service_name)

        self._map_processing_client = robot.ensure_client(
            MapProcessingServiceClient.default_service_name)

        # Store the most recent knowledge of the state of the robot based on rpc calls.
        self._current_graph = None
        self._current_edges = dict()  #maps to_waypoint to list(from_waypoint)
        self._current_waypoint_snapshots = dict()  # maps id to waypoint snapshot
        self._current_edge_snapshots = dict()  # maps id to edge snapshot
        self._current_annotation_name_to_wp_id = dict()

        # Add recording service properties to the command line dictionary.
        self._command_dictionary = {
            '0': self._clear_map,
            '1': self._start_recording,
            '2': self._stop_recording,
            '3': self._get_recording_status,
            '4': self._create_default_waypoint,
            '5': self._download_full_graph,
            '6': self._list_graph_waypoint_and_edge_ids,
            '7': self._create_new_edge,
            '8': self._create_loop,
            '9': self._auto_close_loops_prompt,
            'a': self._optimize_anchoring,
            'o': self._add_object
        }
        self._image_source='frontleft_fisheye_image'
        self._object_cap_dict={
            '0':self._create_waypoint_and_capt_obj_node,
            '1':self._capt_object
        }
        self.g_image_clicks=[]
        self.g_image_display=[]

    def should_we_start_recording(self):
        # Before starting to record, check the state of the GraphNav system.
        graph = self._graph_nav_client.download_graph()
        if graph is not None:
            # Check that the graph has waypoints. If it does, then we need to be localized to the graph
            # before starting to record
            if len(graph.waypoints) > 0:
                localization_state = self._graph_nav_client.get_localization_state()
                if not localization_state.localization.waypoint_id:
                    # Not localized to anything in the map. The best option is to clear the graph or
                    # attempt to localize to the current map.
                    # Returning false since the GraphNav system is not in the state it should be to
                    # begin recording.
                    return False
        # If there is no graph or there exists a graph that we are localized to, then it is fine to
        # start recording, so we return True.
        return True

    def _clear_map(self, *args):
        """Clear the state of the map on the robot, removing all waypoints and edges."""
        return self._graph_nav_client.clear_graph()

    def _start_recording(self, *args):
        """Start recording a map."""
        should_start_recording = self.should_we_start_recording()
        if not should_start_recording:
            print("The system is not in the proper state to start recording.", \
                   "Try using the graph_nav_command_line to either clear the map or", \
                   "attempt to localize to the map.")
            return
        try:
            status = self._recording_client.start_recording(
                recording_environment=self._recording_environment)
            print("Successfully started recording a map.")
        except Exception as err:
            print("Start recording failed: " + str(err))

    def _stop_recording(self, *args):
        """Stop or pause recording a map."""
        first_iter = True
        while True:
            try:
                status = self._recording_client.stop_recording()
                print("Successfully stopped recording a map.")
                break
            except bosdyn.client.recording.NotReadyYetError as err:
                # It is possible that we are not finished recording yet due to
                # background processing. Try again every 1 second.
                if first_iter:
                    print("Cleaning up recording...")
                first_iter = False
                time.sleep(1.0)
                continue
            except Exception as err:
                print("Stop recording failed: " + str(err))
                break

    def _get_recording_status(self, *args):
        """Get the recording service's status."""
        status = self._recording_client.get_record_status()
        if status.is_recording:
            print("The recording service is on.")
        else:
            print("The recording service is off.")

    def _create_default_waypoint(self, *args):
        """Create a default waypoint at the robot's current location."""
        resp = self._recording_client.create_waypoint(waypoint_name="default")
        if resp.status == recording_pb2.CreateWaypointResponse.STATUS_OK:
            print("Successfully created a waypoint.")
        else:
            print("Could not create a waypoint.")

    def _create_custom_waypoint(self,name, *args):
        """Create a custom waypoint at the robot's current location."""
        resp = self._recording_client.create_waypoint(waypoint_name=name)
        if resp.status == recording_pb2.CreateWaypointResponse.STATUS_OK:
            print("Successfully created waypoint- "+name)
        else:
            print("Could not create waypoint- "+name)

    def _download_full_graph(self, *args):
        """Download the graph and snapshots from the robot."""
        graph = self._graph_nav_client.download_graph()
        if graph is None:
            print("Failed to download the graph.")
            return
        self._write_full_graph(graph)
        print("Graph downloaded with {} waypoints and {} edges".format(
            len(graph.waypoints), len(graph.edges)))
        # Download the waypoint and edge snapshots.
        self._download_and_write_waypoint_snapshots(graph.waypoints)
        self._download_and_write_edge_snapshots(graph.edges)

    def _write_full_graph(self, graph):
        """Download the graph from robot to the specified, local filepath location."""
        graph_bytes = graph.SerializeToString()
        self._write_bytes(self._download_filepath, '/graph', graph_bytes)

    def _download_and_write_waypoint_snapshots(self, waypoints):
        """Download the waypoint snapshots from robot to the specified, local filepath location."""
        num_waypoint_snapshots_downloaded = 0
        for waypoint in waypoints:
            if len(waypoint.snapshot_id) == 0:
                continue
            try:
                waypoint_snapshot = self._graph_nav_client.download_waypoint_snapshot(
                    waypoint.snapshot_id)
            except Exception:
                # Failure in downloading waypoint snapshot. Continue to next snapshot.
                print("Failed to download waypoint snapshot: " + waypoint.snapshot_id)
                continue
            self._write_bytes(self._download_filepath + '/waypoint_snapshots',
                              '/' + waypoint.snapshot_id, waypoint_snapshot.SerializeToString())
            num_waypoint_snapshots_downloaded += 1
            print("Downloaded {} of the total {} waypoint snapshots.".format(
                num_waypoint_snapshots_downloaded, len(waypoints)))

    def _download_and_write_edge_snapshots(self, edges):
        """Download the edge snapshots from robot to the specified, local filepath location."""
        num_edge_snapshots_downloaded = 0
        num_to_download = 0
        for edge in edges:
            if len(edge.snapshot_id) == 0:
                continue
            num_to_download += 1
            try:
                edge_snapshot = self._graph_nav_client.download_edge_snapshot(edge.snapshot_id)
            except Exception:
                # Failure in downloading edge snapshot. Continue to next snapshot.
                print("Failed to download edge snapshot: " + edge.snapshot_id)
                continue
            self._write_bytes(self._download_filepath + '/edge_snapshots', '/' + edge.snapshot_id,
                              edge_snapshot.SerializeToString())
            num_edge_snapshots_downloaded += 1
            print("Downloaded {} of the total {} edge snapshots.".format(
                num_edge_snapshots_downloaded, num_to_download))

    def _write_bytes(self, filepath, filename, data):
        """Write data to a file."""
        os.makedirs(filepath, exist_ok=True)
        with open(filepath + filename, 'wb+') as f:
            f.write(data)
            f.close()

    def _update_graph_waypoint_and_edge_ids(self, do_print=False):
        # Download current graph
        graph = self._graph_nav_client.download_graph()
        if graph is None:
            print("Empty graph.")
            return
        self._current_graph = graph

        localization_id = self._graph_nav_client.get_localization_state().localization.waypoint_id

        # Update and print waypoints and edges
        self._current_annotation_name_to_wp_id, self._current_edges = graph_nav_util.update_waypoints_and_edges(
            graph, localization_id, do_print)

    def _list_graph_waypoint_and_edge_ids(self, *args):
        """List the waypoint ids and edge ids of the graph currently on the robot."""
        self._update_graph_waypoint_and_edge_ids(do_print=True)

    def _create_new_edge(self, *args):
        """Create new edge between existing waypoints in map."""

        if len(args[0]) != 2:
            print("ERROR: Specify the two waypoints to connect (short code or annotation).")
            return

        self._update_graph_waypoint_and_edge_ids(do_print=False)

        from_id = graph_nav_util.find_unique_waypoint_id(args[0][0], self._current_graph,
                                                         self._current_annotation_name_to_wp_id)
        to_id = graph_nav_util.find_unique_waypoint_id(args[0][1], self._current_graph,
                                                       self._current_annotation_name_to_wp_id)

        print("Creating edge from {} to {}.".format(from_id, to_id))

        from_wp = self._get_waypoint(from_id)
        if from_wp is None:
            return

        to_wp = self._get_waypoint(to_id)
        if to_wp is None:
            return

        # Get edge transform based on kinematic odometry
        edge_transform = self._get_transform(from_wp, to_wp)

        # Define new edge
        new_edge = map_pb2.Edge()
        new_edge.id.from_waypoint = from_id
        new_edge.id.to_waypoint = to_id
        new_edge.from_tform_to.CopyFrom(edge_transform)

        print("edge transform =", new_edge.from_tform_to)

        # Send request to add edge to map
        self._recording_client.create_edge(edge=new_edge)

    def _create_loop(self, *args):
        """Create edge from last waypoint to first waypoint."""

        self._update_graph_waypoint_and_edge_ids(do_print=False)

        if len(self._current_graph.waypoints) < 2:
            self._add_message(
                "Graph contains {} waypoints -- at least two are needed to create loop.".format(
                    len(self._current_graph.waypoints)))
            return False

        sorted_waypoints = graph_nav_util.sort_waypoints_chrono(self._current_graph)
        edge_waypoints = [sorted_waypoints[-1][0], sorted_waypoints[0][0]]

        self._create_new_edge(edge_waypoints)

    def _auto_close_loops_prompt(self, *args):
        print("""
        Options:
        (0) Close all loops.
        (1) Close only fiducial-based loops.
        (2) Close only odometry-based loops.
        (q) Back.
        """)
        try:
            inputs = input('>')
        except NameError:
            return
        req_type = str.split(inputs)[0]
        close_fiducial_loops = False
        close_odometry_loops = False
        if req_type == '0':
            close_fiducial_loops = True
            close_odometry_loops = True
        elif req_type == '1':
            close_fiducial_loops = True
        elif req_type == '2':
            close_odometry_loops = True
        elif req_type == 'q':
            return
        else:
            print("Unrecognized command. Going back.")
            return
        self._auto_close_loops(close_fiducial_loops, close_odometry_loops)

    def _auto_close_loops(self, close_fiducial_loops, close_odometry_loops, *args):
        """Automatically find and close all loops in the graph."""
        response = self._map_processing_client.process_topology(
            params=map_processing_pb2.ProcessTopologyRequest.Params(
                do_fiducial_loop_closure=wrappers.BoolValue(value=close_fiducial_loops),
                do_odometry_loop_closure=wrappers.BoolValue(value=close_odometry_loops)),
            modify_map_on_server=True)
        print("Created {} new edge(s).".format(len(response.new_subgraph.edges)))

    def _optimize_anchoring(self, *args):
        """Call anchoring optimization on the server, producing a globally optimal reference frame for waypoints to be expressed in."""
        response = self._map_processing_client.process_anchoring(
            params=map_processing_pb2.ProcessAnchoringRequest.Params(),
            modify_anchoring_on_server=True, stream_intermediate_results=False)
        if response.status == map_processing_pb2.ProcessAnchoringResponse.STATUS_OK:
            print("Optimized anchoring after {} iteration(s).".format(response.iteration))
        else:
            print("Error optimizing {}".format(response))

    def cv_mouse_callback(self, event, x, y, flags, param):
        
        clone = g_image_display.copy()

        # Handle left button release event
        if event == cv2.EVENT_LBUTTONUP:
            # Prompt for object name
            tk_root = tk.Tk()
            tk_root.withdraw()  # Hide the main window
            object_name = simpledialog.askstring("Input", "Enter object name:", parent=tk_root)
            tk_root.destroy()

            if object_name:
                print("added object name")
                self.g_image_clicks.append({'coords': (x, y), 'name': object_name})
                self._robot.logger.info(f'Object "{object_name}" added at ({x}, {y})')
            else:
                self._robot.logger.info('No object name entered, point not added')

        # Draw lines and markers on the image
        color_line = (30, 30, 30)
        color_marker = (0, 255, 0)
        thickness_line = 2
        thickness_marker = 2
        marker_type = cv2.MARKER_TILTED_CROSS
        image_title = 'Select objects to grasp'
        height, width = clone.shape[:2]

        # Draw current mouse position lines
        cv2.line(clone, (0, y), (width, y), color_line, thickness_line)
        cv2.line(clone, (x, 0), (x, height), color_line, thickness_line)

        # Draw markers for previously clicked points and annotate with names
        for point_info in self.g_image_clicks:
            point = point_info['coords']
            cv2.drawMarker(clone, point, color_marker, marker_type, markerSize=10, thickness=thickness_marker)
            cv2.putText(clone, point_info['name'], (point[0] + 10, point[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color_marker, 1, cv2.LINE_AA)

        cv2.imshow(image_title, clone)


    def _create_waypoint_and_capt_obj_node(self,*args):
        print("capturing waypoint")
        wp_name=input("Enter waypoint name: ")
        self._create_custom_waypoint(wp_name)
        self._capt_object(wp_name)

    def _capt_object(self,waypoint_name=None,*args):
        if(waypoint_name==None):
            print("waypoint name not input")
            return

        graph = self._graph_nav_client.download_graph()
        curr_waypoint=None
        for waypoint in graph.waypoints:
            if waypoint.annotations.name == waypoint_name:
                curr_waypoint=waypoint
        
        #capture objects
        image_client = self._robot.ensure_client(ImageClient.default_service_name)
        image_responses = image_client.get_image_from_sources([self._image_source])

        if len(image_responses) != 1:
            print('Got invalid number of images: ' + str(len(image_responses)))
            print(image_responses)
            assert False

        image = image_responses[0]
        if image.shot.image.pixel_format == image_pb2.Image.PIXEL_FORMAT_DEPTH_U16:
            dtype = np.uint16
        else:
            dtype = np.uint8
        img = np.fromstring(image.shot.image.data, dtype=dtype)
        if image.shot.image.format == image_pb2.Image.FORMAT_RAW:
            img = img.reshape(image.shot.image.rows, image.shot.image.cols)
        else:
            img = cv2.imdecode(img, -1)

        self._robot.logger.info('Click on objects, press "e" to end selection...')
        image_title = 'Select objects to grasp'
        cv2.namedWindow(image_title)
        cv2.setMouseCallback(image_title, self.cv_mouse_callback)

        global g_image_display
        g_image_display = img
        cv2.imshow(image_title, g_image_display)

        while True:
            key = cv2.waitKey(1) & 0xFF
            if key == ord('e') or key == ord('E'):
                # End selection
                print('Ending selection.')
                break
            elif key == ord('q') or key == ord('Q'):
                # Quit
                print('"q" pressed, exiting.')
                exit(0)

        for point in self.g_image_clicks:
            self._robot.logger.info(f'Object at image location ({point[0]}, {point[1]}) added to graph')
            pick_vec = geometry_pb2.Vec2(x=point[0], y=point[1])

            # Build the proto for each point
            grasp = manipulation_api_pb2.PickObjectInImage(
                pixel_xy=pick_vec, transforms_snapshot_for_camera=image.shot.transforms_snapshot,
                frame_name_image_sensor=image.shot.frame_name_image_sensor,
                camera_model=image.source.pinhole)
            for point_info in self.g_image_clicks:
                x, y = point_info['coords']  # Extract coordinates
                object_name = point_info['name']  # Extract object name

                self._robot.logger.info(f'Object "{object_name}" at image location ({x}, {y}) added to graph')
                pick_vec = geometry_pb2.Vec2(x=x, y=y)

                # Build the proto for each point
                grasp = manipulation_api_pb2.PickObjectInImage(
                    pixel_xy=pick_vec, 
                    transforms_snapshot_for_camera=image.shot.transforms_snapshot,
                    frame_name_image_sensor=image.shot.frame_name_image_sensor,
                    camera_model=image.source.pinhole)
                
                waypoint.annotations[object_name].CopyFrom(grasp)

        # Upload the modified graph back to the robot
        self._graph_nav_client.upload_graph(graph)
        print(f"Successfully added objects.")
        
        return True


    def _add_object(self,*args):
        """A simple example of using the Boston Dynamics API to command Spot's arm."""

        
        status = self._recording_client.get_record_status()
        if status.is_recording:
            print("Recording. Proceed to capture object node")
        else:
            print("Start recording to capture object.")
            return
        
        while True:
            print("""
            Options for capturing object:
            (0) create a new waypoint at spot loaction and capture object.
            (1) add object to previous waypoint. (add waypoint to add to specific waypoint)
            (q) Exit.
            """)
            try:
                inputs = input('>')
            except NameError:
                pass
            req_type = str.split(inputs)[0]

            if req_type == 'q':
                break

            if req_type not in self._object_cap_dict:
                print("Request not in the known command dictionary.")
                continue
            try:
                cmd_func = self._object_cap_dict[req_type]
                cmd_func(str.split(inputs)[1:])
            except Exception as e:
                print(e)



        # image_client = self._robot.ensure_client(ImageClient.default_service_name)


        # # Take a picture with a camera
        # self._robot.logger.info('Getting an image from: ' + self._image_source)
        # image_responses = image_client.get_image_from_sources([self._image_source])

        # if len(image_responses) != 1:
        #     print('Got invalid number of images: ' + str(len(image_responses)))
        #     print(image_responses)
        #     assert False

        # image = image_responses[0]
        # if image.shot.image.pixel_format == image_pb2.Image.PIXEL_FORMAT_DEPTH_U16:
        #     dtype = np.uint16
        # else:
        #     dtype = np.uint8
        # img = np.fromstring(image.shot.image.data, dtype=dtype)
        # if image.shot.image.format == image_pb2.Image.FORMAT_RAW:
        #     img = img.reshape(image.shot.image.rows, image.shot.image.cols)
        # else:
        #     img = cv2.imdecode(img, -1)

        # # Show the image to the user and wait for them to click on a pixel
        # self._robot.logger.info('Click on an object to start grasping...')
        # image_title = 'Click to grasp'
        # cv2.namedWindow(image_title)
        # cv2.setMouseCallback(image_title, self.cv_mouse_callback)

        # global g_image_click, g_image_display
        # g_image_display = img
        # cv2.imshow(image_title, g_image_display)
        # while g_image_click is None:
        #     key = cv2.waitKey(1) & 0xFF
        #     if key == ord('q') or key == ord('Q'):
        #         # Quit
        #         print('"q" pressed, exiting.')
        #         exit(0)

        # self._robot.logger.info('Object at image location (' + str(g_image_click[0]) + ', ' +
        #                 str(g_image_click[1]) + ') added to graph')

        # pick_vec = geometry_pb2.Vec2(x=g_image_click[0], y=g_image_click[1])

        # # Build the proto
        # grasp = manipulation_api_pb2.PickObjectInImage(
        #     pixel_xy=pick_vec, transforms_snapshot_for_camera=image.shot.transforms_snapshot,
        #     frame_name_image_sensor=image.shot.frame_name_image_sensor,
        #     camera_model=image.source.pinhole)
        
        # waypoint_name = input('Enter Waypoint Name: ')
        # resp = self._recording_client.create_waypoint(waypoint_name=waypoint_name)
        # if resp.status == recording_pb2.CreateWaypointResponse.STATUS_OK:
        #     print("Successfully created a waypoint= "+waypoint_name)
        # else:
        #     print("Could not create a waypoint.")

        # waypoint_id = resp.waypoint_id

        # # Retrieve the current graph
        # graph = self._graph_nav_client.download_graph()

        # # Find the new waypoint in the graph
        # waypoint = self._find_waypoint_in_graph(graph, waypoint_id)
        # if waypoint is None:
        #     print(f"Waypoint with ID {waypoint_id} not found.")
        #     return False

        
        # # Add annotations to the waypoint
        # # for key, value in annotations_dict.items():
        # #     # The value must be a protobuf message
        # #     annotation = any_pb2.Any()
        # #     annotation.Pack(value)
        # waypoint.annotations[waypoint_name].CopyFrom(grasp)

        # # Upload the modified graph back to the robot
        # self._graph_nav_client.upload_graph(graph)

        # print(f"Successfully created waypoint with ID {waypoint_id} and added annotations.")
        # return True


    def _get_waypoint(self, id):
        """Get waypoint from graph (return None if waypoint not found)"""

        if self._current_graph is None:
            self._current_graph = self._graph_nav_client.download_graph()

        for waypoint in self._current_graph.waypoints:
            if waypoint.id == id:
                return waypoint

        print('ERROR: Waypoint {} not found in graph.'.format(id))
        return None

    def _get_transform(self, from_wp, to_wp):
        """Get transform from from-waypoint to to-waypoint."""

        from_se3 = from_wp.waypoint_tform_ko
        from_tf = SE3Pose(
            from_se3.position.x, from_se3.position.y, from_se3.position.z,
            Quat(w=from_se3.rotation.w, x=from_se3.rotation.x, y=from_se3.rotation.y,
                 z=from_se3.rotation.z))

        to_se3 = to_wp.waypoint_tform_ko
        to_tf = SE3Pose(
            to_se3.position.x, to_se3.position.y, to_se3.position.z,
            Quat(w=to_se3.rotation.w, x=to_se3.rotation.x, y=to_se3.rotation.y,
                 z=to_se3.rotation.z))

        from_T_to = from_tf.mult(to_tf.inverse())
        return from_T_to.to_proto()

    def run(self):
        """Main loop for the command line interface."""
        while True:
            print("""
            Options:
            (0) Clear map.
            (1) Start recording a map.
            (2) Stop recording a map.
            (3) Get the recording service's status.
            (4) Create a default waypoint in the current robot's location.
            (5) Download the map after recording.
            (6) List the waypoint ids and edge ids of the map on the robot.
            (7) Create new edge between existing waypoints using odometry.
            (8) Create new edge from last waypoint to first waypoint using odometry.
            (9) Automatically find and close loops.
            (a) Optimize the map's anchoring.
            (o) Add object node
            (q) Exit.
            """)
            try:
                inputs = input('>')
            except NameError:
                pass
            req_type = str.split(inputs)[0]

            if req_type == 'q':
                break

            if req_type not in self._command_dictionary:
                print("Request not in the known command dictionary.")
                continue
            try:
                cmd_func = self._command_dictionary[req_type]
                cmd_func(str.split(inputs)[1:])
            except Exception as e:
                print(e)


def main(argv):
    """Run the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    bosdyn.client.util.add_base_arguments(parser)
    parser.add_argument('-d', '--download-filepath',
                        help='Full filepath for where to download graph and snapshots.',
                        default=os.getcwd())
    parser.add_argument(
        '-n', '--recording_user_name', help=
        'If a special user name should be attached to this session, use this name. If not provided, the robot username will be used.',
        default='')
    parser.add_argument(
        '-s', '--recording_session_name', help=
        'Provides a special name for this recording session. If not provided, the download filepath will be used.',
        default='')
    options = parser.parse_args(argv)

    # Create robot object.
    sdk = bosdyn.client.create_standard_sdk('RecordingClient')
    robot = sdk.create_robot(options.hostname)
    bosdyn.client.util.authenticate(robot)

    # Parse session and user name options.
    session_name = options.recording_session_name
    if session_name == '':
        session_name = os.path.basename(options.download_filepath)
    user_name = options.recording_user_name
    if user_name == '':
        user_name = robot._current_user

    # Crate metadata for the recording session.
    client_metadata = GraphNavRecordingServiceClient.make_client_metadata(
        session_name=session_name, client_username=user_name, client_id='RecordingClient',
        client_type='Python SDK')
    recording_command_line = RecordingInterface(robot, options.download_filepath, client_metadata)

    try:
        recording_command_line.run()
        return True
    except Exception as exc:  # pylint: disable=broad-except
        print(exc)
        print("Recording command line client threw an error.")
        return False


if __name__ == '__main__':
    if not main(sys.argv[1:]):
        sys.exit(1)
