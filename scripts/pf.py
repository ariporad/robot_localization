#!/usr/bin/env python3

""" This is the starter code for the robot localization project """

from collections import namedtuple
from itertools import islice

import matplotlib.pyplot as plt

from pathlib import Path
import math
import time
from typing import Iterable, Optional, Tuple, List
import rospy
import random
from nav_msgs.msg import Odometry
from nav_msgs.srv import GetMap
from std_msgs.msg import Header
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import PoseWithCovarianceStamped, PoseArray, PoseStamped, Pose
import tf2_ros
import tf2_geometry_msgs
from tf.transformations import euler_from_quaternion

import numpy as np
from numpy.random import default_rng, Generator

from helper_functions import TFHelper, print_time, sample_normal, sample_normal_error
from occupancy_field import OccupancyField

# NB: All particles are in the `map` frame
rng: Generator = default_rng()


class Particle(namedtuple('Particle', ['x', 'y', 'theta', 'weight'])):
    def __repr__(self):
        return f"Particle(x={self.x:.3f}, y={self.y:.3f}, theta={self.theta:.3f}, w={self.weight:.6f})"


class PoseTuple(namedtuple('PoseTuple', ['x', 'y', 'theta'])):
    def __repr__(self):
        return f"PoseTuple(x={self.x:.3f}, y={self.y:.3f}, theta={self.theta:.3f})"


"""
# The Plan

We need:
- Initial State Model: P(x0)
    - initialpose topic + uncertainty
- Motion Model (odometry): P(Xt | X(t-1), Ut)
    - This needs to be odometry + uncertainty
- Sensor Model: P(Zt | xt)
    - Based on occupancy model + flat noise uncertainty + normal distribution

Setup:
1. Create initial particles:
    - Weighted random sample from p(x0)
       - How to do 2d weighted random sample properly?
       - We're going to do this wrong to start, by doing X and Y seperately

Repeat:
2. Resample particles, using weights as the distribution
3. Update each particle with the motion model (odometry)
    - Figure out where odom is now (convert (0,0,0):base_link -> odom)
    - Compare that with last cycle to get delta_odom
    - For each particle, update x/y/theta by a random sample of delta_odom
4. Compute weights: likelyhood that we would have gotten the laser data if we were at each particle
    - Use the occupancy field for this
    - Normalize weights to 1
5. Goto Step 2

Notes:
- Convention for normal distributions: sigma is stddev, noise the proportion of time to pick a random value

"""


class ParticleFilter:
    """
    The class that represents a Particle Filter ROS Node
    """

    INITIAL_STATE_XY_SIGMA = 0.25
    INITIAL_STATE_XY_NOISE = 0.01

    INITIAL_STATE_THETA_SIGMA = 0.15 * math.pi
    INITIAL_STATE_THETA_NOISE = 0.00

    NUM_PARTICLES = 300

    particles: List[Particle] = None
    robot_pose: PoseStamped = None

    last_pose: PoseTuple = None
    last_lidar: Optional[LaserScan] = None

    map_obstacles: np.array
    tf_listener: tf2_ros.TransformListener

    is_updating: bool = False

    def __init__(self):
        rospy.init_node('pf')
        self.last_update = rospy.Time.now()
        self.last_update_real = rospy.Time.now()

        self.update_count = 0

        # create instances of two helper objects that are provided to you
        # as part of the project
        self.occupancy_field = OccupancyField()  # NOTE: hangs if a map isn't published
        self.transform_helper = TFHelper()
        self.tf_buf = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buf)

        # pose_listener responds to selection of a new approximate robot
        # location (for instance using rviz)
        rospy.Subscriber("initialpose",
                         PoseWithCovarianceStamped,
                         self.update_initial_pose)

        rospy.Subscriber("odom", Odometry, self.update)
        rospy.Subscriber("stable_scan", LaserScan, self.on_lidar)

        # publisher for the particle cloud for visualizing in rviz.
        self.map_pub = rospy.Publisher("parsed_map",
                                       PoseArray,
                                       queue_size=10)
        self.particle_pub = rospy.Publisher("particlecloud",
                                            PoseArray,
                                            queue_size=10)

        self.preprocess_map()

    def update_initial_pose(self, msg: PoseWithCovarianceStamped):
        """ Callback function to handle re-initializing the particle filter
            based on a pose estimate.  These pose estimates could be generated
            by another ROS Node or could come from the rviz GUI """
        x, y, theta = self.transform_helper.convert_pose_to_xy_and_theta(
            msg.pose.pose)

        particles = self.sample_particles(
            [Particle(x, y, theta, 1)],
            self.INITIAL_STATE_XY_SIGMA, self.INITIAL_STATE_XY_NOISE, self.INITIAL_STATE_THETA_SIGMA, self.INITIAL_STATE_THETA_NOISE, self.NUM_PARTICLES)

        self.set_particles(msg.header.stamp, particles)

    def update(self, msg: Odometry):
        if self.is_updating or msg.header.stamp < self.last_update_real:
            return
        self.is_updating = True
        did_anything = False
        start_time = time.perf_counter()
        try:
            # last_odom = self.last_odom
            # translation, orientation_q = self.transform_helper.convert_pose_inverse_transform(
            #     msg.pose.pose)
            # orientation = euler_from_quaternion(orientation_q)[2]
            # odom = (translation[0], translation[1], orientation)

            # cur_pose_bl = PoseStamped()
            # cur_pose_bl.pose.orientation.w = 1.0
            # cur_pose_bl.header.frame_id = 'base_link'
            # cur_pose_bl.header.stamp = msg.header.stamp
            # pose_odom = self.tf_buf.transform(
            #     cur_pose_bl, 'odom', rospy.Duration(0))
            cur_pose = PoseTuple(*self.transform_helper.convert_pose_to_xy_and_theta(
                msg.pose.pose))
            # pose_odom.pose))

            if self.last_pose is None or self.particles is None:
                self.last_pose = cur_pose
                return

            delta_pose = PoseTuple(
                self.last_pose[0] - cur_pose[0],
                self.last_pose[1] - cur_pose[1],
                self.transform_helper.angle_diff(
                    self.last_pose[2], cur_pose[2])
            )

            # Make sure we've moved at least a bit
            if math.sqrt((delta_pose[0] ** 2) + (delta_pose[1] ** 2)) < 0.01 and delta_pose[2] < 0.05:
                return

            print("Delta Pose:", delta_pose)
            did_anything = True

            # now = rospy.Time.now()

            # Resample Particles
            particles = self.sample_particles(
                self.particles,
                self.INITIAL_STATE_XY_SIGMA, self.INITIAL_STATE_XY_NOISE, self.INITIAL_STATE_THETA_SIGMA, self.INITIAL_STATE_THETA_NOISE, self.NUM_PARTICLES)
            # particles = list(self.particles)

            # Apply Motion
            particles = self.apply_motion(particles, delta_pose, 0.15)

            particles = [
                Particle(p.x, p.y, p.theta, self.calculate_sensor_weight(p))
                for p in particles
            ]

            # print("Particle weights:", sorted([p.weight for p in particles]))

            self.last_pose = cur_pose
            self.set_particles(msg.header.stamp, particles)
            self.last_update_real = rospy.Time.now()
        finally:
            if did_anything:
                print(
                    f"Update took {(time.perf_counter() - start_time) * 1000:.2f}ms.\n")
            self.is_updating = False

    def calculate_sensor_weight(self, particle: Particle, save=False, save_name=None) -> float:
        # I think this is broken
        # Try debugging by visualizing markers with weight
        if self.last_lidar is None:
            return 1.0

        if save:
            fig, ax = plt.subplots(subplot_kw={'projection': 'polar'})
            # https://stackoverflow.com/a/18486470
            ax.set_theta_offset(math.pi/2.0)
            ax.grid(True)
            # plt.arrow(0, 0, 0, 1)
            ax.arrow(0, 0, 0, 1)

        actual_lidar = np.array(self.last_lidar.ranges[:-1])

        # Take map data as cartesian coords
        # Shift to center at particle
        # NB: Both the map and all particles are in the `map` frame
        obstacles_shifted = self.map_obstacles - [particle.x, particle.y]

        # Convert to polar, and descritize to whole angle increments [0-359]
        rho = np.sqrt(
            (obstacles_shifted[:, 0] ** 2) + (obstacles_shifted[:, 1] ** 2)
        )
        phi_rad = np.arctan2(obstacles_shifted[:, 1], obstacles_shifted[:, 0])

        # Rotate by the particle's heading
        phi_rad += particle.theta

        # Now convert to degrees
        # This is the only place we use degrees, but it's helpful since LIDAR is indexed by degree
        # arctan2(sin(), cos()) normalizes to [-pi, pi]
        phi = np.rad2deg(np.arctan2(np.sin(phi_rad), np.cos(phi_rad))).round()

        if save:
            ax.plot(phi_rad, rho, 'b,')

        # Take the minimum at each angle
        # Indexed like lidar data, where each index is the degree
        expected_lidar = np.empty(360)
        expected_lidar[:] = np.NaN

        for phi, rho in zip(phi, rho):
            # phi is already an integer, just make the type right
            idx = int(phi)

            # Don't care if we don't have any LIDAR data

            # 10 is (IIRC) the max range of the LIDAR sensor. Anything more than that comes as null
            # if rho > 10.0:
            #     continue

            if rho > 3.0:
                rho = 0.0

            if np.isnan(expected_lidar[idx]) or rho < expected_lidar[idx]:
                expected_lidar[idx] = rho

        expected_lidar[np.isnan(expected_lidar)] = 0.0  # ???
        # print(expected_lidar)

        if save:
            ax.plot(np.deg2rad(np.arange(0, 360)), expected_lidar, 'c.')
            ax.plot(np.deg2rad(np.arange(0, 360)), actual_lidar, 'r.')

        # print("Calculated Map Polar Data:", expected_lidar)

        # Compare to LIDAR data (don't forget to drop the extra point #360)
        mask = actual_lidar > 0.0
        diff_lidar = np.abs(actual_lidar - expected_lidar)

        # if save:
        #     ax.plot(np.deg2rad(np.arange(0, 360))[
        #             mask], diff_lidar[mask], 'g.')

        weight = np.sum((1 / diff_lidar[diff_lidar > 0.0]) ** 3)

        if save:
            ax.set_title(
                f"({particle.x:.2f}, {particle.y:.2f}; {particle.theta:.2f}; w: {weight:.6f})"
            )
            data_dir = Path(__file__).parent.parent / 'particle_sensor_data'
            data_dir.mkdir(exist_ok=True)
            fig.savefig(data_dir / f"{save_name}_{weight:010.6f}.png")
            plt.close(fig)

        return weight

    def preprocess_map(self):
        rospy.wait_for_service("static_map")
        static_map = rospy.ServiceProxy("static_map", GetMap)
        map = static_map().map

        if map.info.origin.orientation.w != 1.0:
            print("WARNING: Unsupported map with rotated origin.")

        # The coordinates of each occupied grid cell in the map
        total_occupied = np.sum(np.array(map.data) > 0)
        occupied = np.zeros((total_occupied, 2))
        curr = 0
        for x in range(map.info.width):
            for y in range(map.info.height):
                # occupancy grids are stored in row major order
                ind = x + y*map.info.width
                if map.data[ind] > 0:
                    occupied[curr, 0] = (float(x) * map.info.resolution) \
                        + map.info.origin.position.x
                    occupied[curr, 1] = (float(y) * map.info.resolution) \
                        + map.info.origin.position.y
                    curr += 1
        self.map_obstacles = occupied

        # poses = PoseArray()
        # poses.header.stamp = rospy.Time.now()
        # poses.header.frame_id = 'map'
        # poses.poses = [
        #     self.transform_helper.convert_xy_and_theta_to_pose((x, y, 0))
        #     for x, y in occupied
        # ]
        # self.map_pub.publish(poses)

    def apply_motion(self, particles: List[Particle], delta_pose: PoseTuple, sigma: float) -> List[Particle]:
        # If a particle has a heading of theta
        # ihat(t-1) = [cos(theta), sin(theta)]
        # jhat(t-1) = [-sin(theta), cos(theta)]
        # x(t) = ihat(t) * delta.x + jhat(t)

        # print(
        #     f"delta_pose.x={delta_pose.x}, delta_pose.y={delta_pose.y}, delta_pose.theta={delta_pose.theta}")

        dx_robot = sample_normal_error(delta_pose.x, sigma)
        dy_robot = sample_normal_error(delta_pose.y, sigma)
        dtheta = sample_normal_error(delta_pose.theta, sigma)

        # print(f"dx_r={dx_robot}, dy_r={dy_robot}, dtheta={dtheta}")

        rot_dtheta = np.array([
            [np.cos(dtheta), -np.sin(dtheta)],
            [np.sin(dtheta), np.cos(dtheta)]
        ])

        dx, dy = np.matmul(rot_dtheta, [dx_robot, dy_robot])

        return [
            Particle(
                x=p.x + dx,
                y=p.y + dy,
                theta=p.theta - dtheta,
                weight=p.weight
            )
            for p in particles
        ]

    def sample_particles(self, particles: List[Particle], xy_sigma: float, xy_noise: float, theta_sigma: float, theta_noise: float, k: int) -> List[Particle]:
        weights = [p.weight for p in particles]
        # print("WEIGHTS:", sorted(weights))
        choices = random.choices(
            particles,
            weights=weights,
            k=k
        )

        return [
            Particle(
                x=sample_normal(choice.x, xy_sigma, xy_noise,
                                (choice.x - 5, choice.x + 5)),
                y=sample_normal(choice.y, xy_sigma, xy_noise,
                                (choice.y - 5, choice.y + 5)),
                theta=sample_normal(choice.theta, theta_sigma,
                                    theta_noise, (-math.pi, math.pi)),
                weight=1
            )
            for choice in choices
        ]

    def set_particles(self, stamp: rospy.Time, particles: List[Particle]):
        self.last_update = stamp
        # self.particles = particles
        self.particles = self.normalize_weights(particles)

        # if self.tf_buf.can_transform('base_link', 'odom', stamp, rospy.Duration(1)) or True:
        # Calculate robot pose / map frame
        # NB: Particles are always in the map reference frame
        robot_pose = self.transform_helper.convert_xy_and_theta_to_pose(np.average([  # TODO: should this be median?
            (particle.x, particle.y, particle.theta)
            for particle in self.particles
        ], axis=0, weights=[p.weight for p in particles]))
        # robot_pose is where the robot is in map
        # By definition, that's also (0, 0, 0) in base_link
        # So the inverse of robot_pose is the transformation between base_link and map
        # Subtract whatever the tranformation from base_link to odom is, and you get odom->map

        self.transform_helper.fix_map_to_odom_transform(stamp, robot_pose)

        # Publish particles
        poses = PoseArray()
        poses.header.stamp = stamp
        poses.header.frame_id = 'map'

        particles = list(
            sorted(list(random.choices(self.particles, k=30)), key=lambda p: p.weight))
        for i, particle in enumerate(particles):
            # for particle in self.particles:
            poses.poses.append(
                self.transform_helper.convert_xy_and_theta_to_pose(
                    (particle.x, particle.y, particle.theta)
                ))
            # self.calculate_sensor_weight(
            #     particle, save=True, save_name=f"particle_{self.update_count:03d}")
        self.update_count += 1

        self.particle_pub.publish(poses)

    def normalize_weights(self, particles: List[Particle]):
        total = sum(p.weight for p in particles)
        return [
            Particle(p.x, p.y, p.theta, p.weight / total)
            for p in particles
        ]

    def on_lidar(self, msg: LaserScan):
        self.last_lidar = msg

    def run(self):
        r = rospy.Rate(5)

        while not rospy.is_shutdown():
            # in the main loop all we do is continuously broadcast the latest
            # map to odom transform
            self.transform_helper.send_last_map_to_odom_transform()
            r.sleep()


if __name__ == '__main__':
    n = ParticleFilter()
    n.run()
