
import rospy
import ros_numpy
import numpy as np
import copy
import json
import os
import sys
import torch
import time 
import argparse
import glob
import math
from pathlib import Path

from std_msgs.msg import Header
from pyquaternion import Quaternion
import sensor_msgs.point_cloud2 as pc2
from sensor_msgs.msg import PointCloud2, PointField
from jsk_recognition_msgs.msg import BoundingBox, BoundingBoxArray
from visualization_msgs.msg import MarkerArray, Marker
from t4ac_perception_msgs.msg import bev_obstacle, bev_obstacles_list, bev_obstacle_3D, bev_obstacles_3D_list


from pcdet.datasets import DatasetTemplate
from pcdet.models import build_network, load_data_to_gpu
from pcdet.config import cfg, cfg_from_yaml_file
from pcdet.utils import common_utils


class DemoDataset(DatasetTemplate):
    def __init__(self, dataset_cfg, class_names, training=True, root_path=None, logger=None, ext='.bin'):
        """
        Args:
            root_path:
            dataset_cfg:
            class_names:
            training:
            logger:
        """
        super().__init__(
            dataset_cfg=dataset_cfg, class_names=class_names, training=training, root_path=root_path, logger=logger
        )
        self.root_path = root_path
        self.ext = ext
        data_file_list = glob.glob(str(root_path / f'*{self.ext}')) if self.root_path.is_dir() else [self.root_path]

        data_file_list.sort()
        self.sample_file_list = data_file_list

    def __len__(self):
        return len(self.sample_file_list)

    def __getitem__(self, index):
        if self.ext == '.bin':
            points = np.fromfile(self.sample_file_list[index], dtype=np.float32).reshape(-1, 4)
        elif self.ext == '.npy':
            points = np.load(self.sample_file_list[index])
        else:
            raise NotImplementedError

        input_dict = {
            'points': points,
            'frame_id': index,
        }

        data_dict = self.prepare_data(data_dict=input_dict)
        return data_dict

def yaw2quaternion(yaw: float) -> Quaternion:
    return Quaternion(axis=[0,0,1], radians=yaw)

def get_annotations_indices(types, thresh, label_preds, scores):
    indexs = []
    annotation_indices = []
    for i in range(label_preds.shape[0]):
        if label_preds[i] == types:
            indexs.append(i)
    for index in indexs:
        if scores[index] >= thresh:
            annotation_indices.append(index)
    return annotation_indices  


def remove_low_score_nu(image_anno, thresh):
    img_filtered_annotations = {}
    label_preds_ = image_anno["label_preds"].detach().cpu().numpy()
    scores_ = image_anno["scores"].detach().cpu().numpy()
    
    car_indices =                  get_annotations_indices(0, 0.45, label_preds_, scores_)
    truck_indices =                get_annotations_indices(1, 0.45, label_preds_, scores_)
    construction_vehicle_indices = get_annotations_indices(2, 0.45, label_preds_, scores_)
    bus_indices =                  get_annotations_indices(3, 0.35, label_preds_, scores_)
    trailer_indices =              get_annotations_indices(4, 0.4, label_preds_, scores_)
    barrier_indices =              get_annotations_indices(5, 0.4, label_preds_, scores_)
    motorcycle_indices =           get_annotations_indices(6, 0.15, label_preds_, scores_)
    bicycle_indices =              get_annotations_indices(7, 0.15, label_preds_, scores_)
    pedestrain_indices =           get_annotations_indices(8, 0.10, label_preds_, scores_)
    traffic_cone_indices =         get_annotations_indices(9, 0.1, label_preds_, scores_)
    
    for key in image_anno.keys():
        if key == 'metadata':
            continue
        img_filtered_annotations[key] = (
            image_anno[key][car_indices +
                            pedestrain_indices + 
                            bicycle_indices +
                            bus_indices +
                            construction_vehicle_indices +
                            traffic_cone_indices +
                            trailer_indices +
                            barrier_indices +
                            truck_indices
                            ])

    return img_filtered_annotations


class Processor_ROS:
    def __init__(self, config_path, model_path):
        self.points = None
        self.config_path = config_path
        self.model_path = model_path
        self.device = None
        self.net = None
        self.voxel_generator = None
        self.inputs = None
        
    def initialize(self):
        self.read_config()
        
    def read_config(self):
        config_path = self.config_path
        cfg_from_yaml_file(self.config_path, cfg)
        self.logger = common_utils.create_logger()
        self.demo_dataset = DemoDataset(
            dataset_cfg=cfg.DATA_CONFIG, class_names=cfg.CLASS_NAMES, training=False,
            root_path=Path("/home/muzi2045/Documents/project/OpenPCDet/data/kitti/velodyne/000001.bin"),
            ext='.bin')
        
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.net = build_network(model_cfg=cfg.MODEL, num_class=len(cfg.CLASS_NAMES), dataset=self.demo_dataset)
        self.net.load_params_from_file(filename=self.model_path, logger=self.logger, to_cpu=True)
        self.net = self.net.to(self.device).eval()

    def run(self, points):
        t_t = time.time()
        print(f"input points shape: {points.shape}")
        num_features = 4        
        self.points = points.reshape([-1, num_features])
        #print("points", self.points)

        timestamps = np.zeros((len(self.points),1))
        self.points = np.append(self.points, timestamps, axis=1)
        self.points[:,0] += movelidarcenter
        #print("points2", self.points)

        input_dict = {
            'points': self.points,
            'frame_id': 0,
        }

        data_dict = self.demo_dataset.prepare_data(data_dict=input_dict)
        data_dict = self.demo_dataset.collate_batch([data_dict])
        load_data_to_gpu(data_dict)

        torch.cuda.synchronize()
        t = time.time()

        pred_dicts, _ = self.net.forward(data_dict)
        
        torch.cuda.synchronize()
        print(f"inference time: {time.time() - t}")

        boxes_lidar = pred_dicts[0]["pred_boxes"].detach().cpu().numpy()
        scores = pred_dicts[0]["pred_scores"].detach().cpu().numpy()
        types = pred_dicts[0]["pred_labels"].detach().cpu().numpy()

        # print(f" pred boxes: { boxes_lidar }")
        # print(f" pred_scores: { scores }")
        # print(f" pred_labels: { types }")

        return scores, boxes_lidar, types

def get_xyz_points(cloud_array, remove_nans=True, dtype=np.float):
    '''
    '''
    if remove_nans:
        mask = np.isfinite(cloud_array['x']) & np.isfinite(cloud_array['y']) & np.isfinite(cloud_array['z'])
        cloud_array = cloud_array[mask]

    points = np.zeros(cloud_array.shape + (4,), dtype=dtype)
    points[...,0] = cloud_array['x']
    points[...,1] = cloud_array['y']
    points[...,2] = cloud_array['z']
    return points

def xyz_array_to_pointcloud2(points_sum, stamp=None, frame_id=None):
    '''
    Create a sensor_msgs.PointCloud2 from an array of points.
    '''
    msg = PointCloud2()
    if stamp:
        msg.header.stamp = stamp
    if frame_id:
        msg.header.frame_id = frame_id
    msg.height = 1
    msg.width = points_sum.shape[0]
    msg.fields = [
        PointField('x', 0, PointField.FLOAT32, 1),
        PointField('y', 4, PointField.FLOAT32, 1),
        PointField('z', 8, PointField.FLOAT32, 1)
        # PointField('i', 12, PointField.FLOAT32, 1)
        ]
    msg.is_bigendian = False
    msg.point_step = 12
    msg.row_step = points_sum.shape[0]
    msg.is_dense = int(np.isfinite(points_sum).all())
    msg.data = np.asarray(points_sum, np.float32).tostring()
    return msg


def rotz(t):
    ''' Rotation about the z-axis. '''
    c = np.cos(t)
    s = np.sin(t)
    return np.array([[c,  -s,  0],
                     [s,   c,  0],
                     [0,   0,  1]])


def anno_to_sort(dt_box_lidar, scores, types):

    pp_list = bev_obstacles_list()		##CREO EL MENSAJE
    objects_list = []

    point_cloud_range = cfg.DATA_CONFIG.POINT_CLOUD_RANGE			##LLENO VALORES GENERICOS
    pp_list.header.stamp = rospy.Time.now()
    pp_list.front = point_cloud_range[3]-movelidarcenter
    pp_list.back = point_cloud_range[0]-movelidarcenter
    pp_list.left = point_cloud_range[1]
    pp_list.right = point_cloud_range[4]

    if scores.size != 0:
        for i in range(scores.size):
            if scores[i] > threshold:

                obj         = bev_obstacle()
                #obj.type    = int(types[i])
                obj.score   = scores[i]

                rotation = dt_box_lidar[i][6]

                if rotation > math.pi:
                    rotation = rotation - math.pi
                R = rotz(-rotation)

                # 3d bounding box corners
                l = float(dt_box_lidar[i][3])  #in lidar_frame coordinates
                w = float(dt_box_lidar[i][4])
                location_x = -float(dt_box_lidar[i][1])
                location_y = -(float(dt_box_lidar[i][0]) - movelidarcenter)
                x_corners = [-l/2,-l/2,l/2, l/2]
                y_corners = [ w/2,-w/2,w/2,-w/2]
                z_corners = [0,0,0,0]
                corners_3d = np.dot(R, np.vstack([x_corners,y_corners,z_corners]))[0:2]
                corners_3d = corners_3d + np.vstack([location_x, location_y])

                obj.x           = location_x
                obj.y           = location_y
                obj.tl_br       = [0,0,0,0]     #2D bbox top-left, bottom-right  xy coordinates
                obj.x_corners   = [corners_3d[0,0], corners_3d[0,1], corners_3d[0,2], corners_3d[0,3]]  #Array of x coordinates (upper left, upper right, lower left, lower right)
                obj.y_corners   = [corners_3d[1,0], corners_3d[1,1], corners_3d[1,2], corners_3d[1,3]]
                obj.l           = l             #in lidar_frame coordinates
                obj.w           = w             #in lidar_frame coordinates
                obj.o           = rotation      #in lidar_frame coordinates

                if int(types[i]) == 1:
                    type_obj_str = "Pedestrian"

                elif int(types[i]) == 2:
                    type_obj_str = "Car"

                elif int(types[i]) == 3:
                    type_obj_str = "Cyclist"

                obj.type = type_obj_str

                pp_list.bev_obstacles_list.append(obj)

    return pp_list

def anno_to_3Dsort(dt_box_lidar, types):

    pp_list_3D = bev_obstacles_3D_list()		##CREO EL MENSAJE
    objects_list = []

    point_cloud_range = cfg.DATA_CONFIG.POINT_CLOUD_RANGE			##LLENO VALORES GENERICOS
    pp_list_3D.header.stamp = rospy.Time.now()
    pp_list_3D.front = point_cloud_range[3]-movelidarcenter
    pp_list_3D.back = point_cloud_range[0]-movelidarcenter
    pp_list_3D.left = point_cloud_range[1]
    pp_list_3D.right = point_cloud_range[4]

    if dt_box_lidar.size != 0:
        for i in range(len(dt_box_lidar)):

            obj         = bev_obstacle_3D()
            #obj.type    = int(types[i])
            #obj.score   = scores[i]
            obj.score   = dt_box_lidar[i][-1]

            rotation = dt_box_lidar[i][6]

            if rotation > math.pi:
                rotation = rotation - math.pi
            R = rotz(-rotation)

            # 3d bounding box corners
            l = float(dt_box_lidar[i][3])  #in lidar_frame coordinates
            w = float(dt_box_lidar[i][4])
            h = float(dt_box_lidar[i][5])
            '''
            location_x = -float(dt_box_lidar[i][1])
            location_y = -(float(dt_box_lidar[i][0]) - movelidarcenter)
            location_z = -float(dt_box_lidar[i][2])
            '''
            location_x = (float(dt_box_lidar[i][0]) - movelidarcenter)
            location_y = float(dt_box_lidar[i][1])
            location_z = float(dt_box_lidar[i][2])
            x_corners = [-l/2,-l/2, l/2, l/2,-l/2,-l/2,l/2, l/2]
            y_corners = [ w/2,-w/2, w/2,-w/2, w/2,-w/2,w/2,-w/2]
            z_corners = [-h/2,-h/2,-h/2,-h/2, h/2, h/2,h/2, h/2]
            corners_3d = np.dot(R, np.vstack([x_corners,y_corners,z_corners]))
            corners_3d = corners_3d + np.vstack([location_x, location_y, location_z])

            obj.x            = -location_y
            obj.y            = -location_x
            obj.x_lidar      = float(dt_box_lidar[i][0]) - movelidarcenter
            obj.y_lidar      = float(dt_box_lidar[i][1])
            obj.z_lidar      = float(dt_box_lidar[i][2])
            obj.tl_br        = [0,0,0,0]     #2D bbox top-left, bottom-right  xy coordinates
            obj.x_corners    = [corners_3d[0,0], corners_3d[0,1], corners_3d[0,2], corners_3d[0,3]]  #Array of x coordinates (upper left, upper right, lower left, lower right)
            obj.y_corners    = [corners_3d[1,0], corners_3d[1,1], corners_3d[1,2], corners_3d[1,3]]
            obj.x_corners_3D = [corners_3d[0,0], corners_3d[0,1], corners_3d[0,2], corners_3d[0,3], corners_3d[0,4], corners_3d[0,5], corners_3d[0,6], corners_3d[0,7]]
            obj.y_corners_3D = [corners_3d[1,0], corners_3d[1,1], corners_3d[1,2], corners_3d[1,3], corners_3d[1,4], corners_3d[1,5], corners_3d[1,6], corners_3d[1,7]]
            obj.z_corners_3D = [corners_3d[2,0], corners_3d[2,1], corners_3d[2,2], corners_3d[2,3], corners_3d[2,4], corners_3d[2,5], corners_3d[2,6], corners_3d[2,7]]
            obj.l            = l             #in lidar_frame coordinates
            obj.w            = w             #in lidar_frame coordinates
            obj.h            = h             #in lidar_frame coordinates
            obj.o            = rotation- math.pi/2      #in lidar_frame coordinates

            if int(types[i]) == 1:
                type_obj_str = "Pedestrian"

            elif int(types[i]) == 2:
                type_obj_str = "Car"

            elif int(types[i]) == 3:
                type_obj_str = "Cyclist"

            obj.type = type_obj_str

            pp_list_3D.bev_obstacles_3D_list.append(obj)

    return pp_list_3D

def anno_to_rviz(dt_box_lidar, scores, types, msg):

    MarkerArray_list = MarkerArray()		##CREO EL MENSAJE GENERAL

    if scores.size != 0:
        for i in range(scores.size):
            if scores[i] > threshold:
                obj = Marker()
                obj.header.stamp = rospy.Time.now()
                obj.header.frame_id = msg.header.frame_id
                obj.type = Marker.CUBE
                obj.id = i
                obj.lifetime = rospy.Duration.from_sec(1)
                obj.pose.position.x = float(dt_box_lidar[i][0]) - movelidarcenter
                obj.pose.position.y = float(dt_box_lidar[i][1])
                obj.pose.position.z = float(dt_box_lidar[i][2])
                q = yaw2quaternion(float(dt_box_lidar[i][6]))
                obj.pose.orientation.x = q[1] 
                obj.pose.orientation.y = q[2]
                obj.pose.orientation.z = q[3]
                obj.pose.orientation.w = q[0]
                obj.scale.x = float(dt_box_lidar[i][3])
                obj.scale.y = float(dt_box_lidar[i][4])
                obj.scale.z = float(dt_box_lidar[i][5])
                obj.color.r = 255
                obj.color.a = 0.5
            
                MarkerArray_list.markers.append(obj)

    return MarkerArray_list

def sortbydistance(dt_box_lidar, scores, types):
    arr = np.empty((0,2), float)
    #Find objects with score under threshold
    index = np.where(scores < threshold)
    annos = np.copy(dt_box_lidar)
    annos = np.delete(annos, obj=index, axis=0)
    scores_over_threshold = np.copy(scores)
    scores_over_threshold = np.delete(scores_over_threshold, obj=index, axis=0)
    #Calculate distance between object and vehicle
    for i in range(len(annos)):
        x_lidar = float(annos[i][0]) - movelidarcenter
        y_lidar = float(annos[i][1])
        rho, phi = cart2pol(x_lidar, y_lidar)
        arr = np.append(arr, np.array([[rho, phi]]), axis=0)
    #Add column values to array
    annos_with_distance = np.append(annos, arr, axis=1)
    scores_over_threshold = scores_over_threshold.reshape((-1,1))
    annos_with_distance = np.append(annos_with_distance, scores_over_threshold, axis=1)
    #Sort by distance (rho)
    annos_sorted = annos_with_distance[annos_with_distance[:,-3].argsort()]
    print("annos_sorted", annos_sorted)
    return annos_sorted


def cart2pol(x, y):
        rho = np.sqrt(x**2 + y**2)
        phi = np.arctan2(y, x)
        return(rho, phi)

def rslidar_callback(msg):
    t_t = time.time()
    arr_bbox = BoundingBoxArray()

    msg_cloud = ros_numpy.point_cloud2.pointcloud2_to_array(msg)
    np_p = get_xyz_points(msg_cloud, True)
    print("  ")
    scores, dt_box_lidar, types = proc_1.run(np_p)

    annos_sorted = sortbydistance(dt_box_lidar, scores, types)
    pp_list          = anno_to_sort(dt_box_lidar, scores, types)
    pp_3D_list       = anno_to_3Dsort(annos_sorted, types)
    MarkerArray_list = anno_to_rviz(dt_box_lidar, scores, types, msg)

    if scores.size != 0:
        for i in range(scores.size):
            if scores[i] > threshold:
                bbox = BoundingBox()
                bbox.header.frame_id = msg.header.frame_id
                bbox.header.stamp = rospy.Time.now()
                q = yaw2quaternion(float(dt_box_lidar[i][6]))
                bbox.pose.orientation.x = q[1]
                bbox.pose.orientation.y = q[2]
                bbox.pose.orientation.z = q[3]
                bbox.pose.orientation.w = q[0]           
                bbox.pose.position.x = float(dt_box_lidar[i][0]) - movelidarcenter
                bbox.pose.position.y = float(dt_box_lidar[i][1])
                bbox.pose.position.z = float(dt_box_lidar[i][2])
                bbox.dimensions.x = float(dt_box_lidar[i][3])
                bbox.dimensions.y = float(dt_box_lidar[i][4])
                bbox.dimensions.z = float(dt_box_lidar[i][5])
                bbox.value = scores[i]
                bbox.label = int(types[i])
                arr_bbox.boxes.append(bbox)


    print("total callback time: ", time.time() - t_t)
    arr_bbox.header.frame_id = msg.header.frame_id
    arr_bbox.header.stamp = msg.header.stamp
    if len(arr_bbox.boxes) is not 0:
        pub_arr_bbox.publish(arr_bbox)
        arr_bbox.boxes = []
    else:
        arr_bbox.boxes = []
        pub_arr_bbox.publish(arr_bbox)

    pubRviz.publish(MarkerArray_list)
    pubSort.publish(pp_list)
    pub3DSort.publish(pp_3D_list)
   
if __name__ == "__main__":

    global proc

    config_path = 'cfgs/kitti_models/pointpillar.yaml'
    model_path  = 'cfgs/kitti_models/pointpillar_7728.pth'
    '''
    config_path = 'cfgs/kitti_models/pp_multihead.yaml'
    model_path  = 'cfgs/kitti_models/pp_multihead_nds5823.pth'
    ''' 

    movelidarcenter = 0 #69.12/2
    threshold = 0.4

    proc_1 = Processor_ROS(config_path, model_path)
    
    proc_1.initialize()
    
    rospy.init_node('centerpoint_ros_node')
    sub_lidar_topic = [ "/velodyne_points", 
                        "/carla/ego_vehicle/lidar/lidar1/point_cloud",
                        "/kitti_player/hdl64e", 
                        "/lidar_protector/merged_cloud", 
                        "/merged_cloud",
                        "/lidar_top", 
                        "/roi_pclouds",
                        "/livox/lidar",
                        "/SimOneSM_PointCloud_0"]

    cfg_from_yaml_file(config_path, cfg)
    
    sub_ = rospy.Subscriber(sub_lidar_topic[2], PointCloud2, rslidar_callback, queue_size=1, buff_size=2**24)
    
    pub_arr_bbox = rospy.Publisher("pp_boxes", BoundingBoxArray, queue_size=1)
    pubRviz   = rospy.Publisher('/pp_markers', MarkerArray, queue_size=10)
    pubSort   = rospy.Publisher("/perception/object_detector/bev_detections", bev_obstacles_list, queue_size=10)
    pub3DSort = rospy.Publisher("/pointpillars/bev_detections_3D", bev_obstacles_3D_list, queue_size=10)

    print("[+] PCDet ros_node has started!")    
    rospy.spin()
