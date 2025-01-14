import logging

# import json
import os

# import pathlib
import random
import time

# import math
from typing import Any
from typing import List
from typing import Dict
from typing import Optional
from typing import Tuple
from typing import Union

import gym

# import matplotlib.path as mplPath
import numpy as np
from gym.spaces import Box

# from scipy.spatial import KDTree

from l2r.core import ActionInterface
from l2r.core import CameraInterface
from l2r.core import PoseInterface
from l2r.constants import (
    CAR_DIMS,
    N_SEGMENTS,
    OBS_DELAY,
    RACETRACKS,
    MEDIUM_DELAY,
    LEVEL_Z_DICT,
    COORD_MULTIPLIER,
)
from l2r.utils.space import convert_ll_to_enu
from l2r.track import load_track

from .controller import SimulatorController
from .reward import GranTurismo, CustomReward
from .tracker import ProgressTracker

# from l2r.track import level_2_trackmap

# import ipdb as pdb


class RacingEnv(gym.Env):
    """A reinforcement learning environment for autonomous racing."""

    def __init__(
        self,
        controller: SimulatorController,
        action_interface: ActionInterface,
        camera_interfaces: List[CameraInterface],
        pose_interface: PoseInterface,
        observation_delay: float = OBS_DELAY,
        reward_kwargs: Dict[str, Any] = dict(),
        env_ip: str = "0.0.0.0",
        env_kwargs: Dict[str, Any] = dict(),
        zone=False,
        provide_waypoints=False,
        manual_segments=False,
    ):

        self.manual_segments = manual_segments
        self.provide_waypoints = (
            provide_waypoints if provide_waypoints else env_kwargs["provide_waypoints"]
        )
        self.zone = zone  # currently not supported; future

        self.evaluate = env_kwargs["eval_mode"]
        print("[Env] Evaluate", self.evaluate)

        # global config mappings
        self.n_eval_laps = env_kwargs["n_eval_laps"]
        self.max_timesteps = env_kwargs["max_timesteps"]
        self.not_moving_timeout = env_kwargs["not_moving_timeout"]
        self.observation_delay = env_kwargs["obs_delay"]
        self.reward_pol = env_kwargs["reward_pol"]

        self.vehicle_params = env_kwargs["vehicle_params"]
        self.sensors = env_kwargs["active_sensors"]

        # Interfaces with the simulator
        self.controller = controller
        self.action_interface = action_interface
        self.camera_interfaces = camera_interfaces
        self.pose_interface = pose_interface
        self.reward = (
            GranTurismo(**reward_kwargs)
            if self.reward_pol == "default"
            else CustomReward(**reward_kwargs)
        )

        # delay between action and observation
        self.observation_delay = observation_delay

        # ip address of the env
        self.env_ip = env_ip

        # openAI gym compliance - action space
        self.action_space = Box(low=-1.0, high=1.0, shape=(2,), dtype=np.float64)

        # misc
        self.last_restart = time.time()

    def make(self, levels: List[str], evaluate: Optional[bool] = False):
        """This sequence of steps must be run when first interacting with the
        simulator - including if the simulator process was restarted. In particular,
        the cameras, sensors, and vehicle need to be configured.
        """
        logging.info("Making l2r environment")

        # Set the level in the simulator
        if type(levels) == str:
            self.level = levels
            self.levels = None
            self.active_level = levels
        elif type(levels) == list:
            self.levels = levels
            self.active_level = random.choice(self.levels)
        else:
            self.levels = None
            self.active_level = self.level
        self.controller.set_level(self.active_level)
        self.controller.set_api_udp()

        # Load active map
        self._load_map()

        # Configure driver
        self.controller.set_sensor_params(
            sensor="ArrivalVehicleDriver",
            params={
                "DriverAPIClass": "VApiUdp",
                "DriverAPI_UDP_SendAddress": self.env_ip,
                "InputSource": "AI",
            },
        )

        # Camera configuration
        for camera_if in self.camera_interfaces:
            _ = self.controller.enable_sensor(camera_if.camera_name)
            _ = self.controller.set_sensor_params(
                sensor=camera_if.camera_name, params=camera_if.camera_param_dict
            )
            camera_if.start()

        """for sensor in self.sensors:
            self.controller.enable_sensor(sensor)"""

        # Start pose interface
        self.pose_interface.start()

        return self

    def step(self, action):
        """The primary method of the environment. Executes the desired action,
        receives the observation from the simulator, and evaluates termination
        conditions.

        :param dict action: the action and acceleration requests
        :return: observation, reward, done, info
        :rtype: if multimodal, the observation is a dict of numpy arrays with
          keys 'pose' and 'img' and shapes (30,) and (height, width, 3),
          respectively, otherwise the observation is just the image array.
          reward is of type float, done bool, and info dict
        """

        # Send the action via the action interface
        self.action_interface.act(action)

        # Receive data from the simulator
        observation = self._observe()
        _data = observation["pose"]

        # Check if the episode is complete
        done, info = self._is_complete()

        # Calculate reward of the current state
        reward = self.reward.get_reward(
            state=(_data, self.nearest_idx), oob_flag=info.get("oob", False)
        )

        if self.provide_waypoints:
            print(
                f"[Env] WARNING: 'self.provide_waypoints' \
                    is set to {self.provide_waypoints}"
            )
            info["track_idx"] = self.nearest_idx
            info["waypoints"] = self._waypoints()

        return observation, reward, done, info

    def reset(
        self,
        level: Optional[str] = None,
        random_pos: Optional[bool] = False,
        segment_pos: Optional[bool] = True,
    ):
        """Resets the vehicle to start position. A small time delay is used
        allow for the simulator to reset.

        :param str level: if specified, will set the simulator to this level,
          otherwise set to a random track
        :param bool random_pos: true/false for random starting position on the track
        :param bool segment_pos: true/false for track starting positions that adhere
          to segment boundaries
        :return: an intial observation as in the *step* method
        :rtype: see **step()** method
        """

        if level:
            new_level = level
            logging.info(f"[Env] Setting to level: {new_level}")
        elif self.levels:
            new_level = random.choice(self.levels)
            logging.info(f"[Env] New random level: {new_level}")
        else:
            new_level = self.level
            logging.info(f"[Env] Continuing with level: {new_level}")

        if new_level is self.active_level:
            self.controller.reset_level()

        else:
            self.active_level = new_level
            self.controller.set_level(self.active_level)
            self._load_map()

        self.controller.set_mode_ai()
        self.nearest_idx = None
        # info = {}

        # give the simulator time to reset
        time.sleep(MEDIUM_DELAY)

        # randomly initialize starting location
        p = np.random.uniform()
        # with prob 1/(1+n) use the default start location.
        if (
            (random_pos)
            and (p > 2 / (1 + len(self.racetrack.random_poses)))
            and not self.evaluate
        ):
            coords, rot = self.random_start_location()
            self.controller.set_location(coords, rot)
            time.sleep(MEDIUM_DELAY)

        elif segment_pos and self.evaluate:
            coords, rot = self.next_segment_start_location()
            self.controller.set_location(coords, rot)
            time.sleep(MEDIUM_DELAY)
        else:
            pass

        # set location
        # self.controller.set_location(coords, rot)
        self.tracker.wrong_way = False  # reset
        self.tracker.idx_sequence = [0] * 5  # reset

        # reset simulator sensors
        self.reward.reset()
        self.pose_interface.reset()

        if self.vehicle_params:
            self.controller.set_vehicle_params(self.vehicle_params)

        for sensor in self.sensors:
            self.controller.enable_sensor(sensor)

        for camera in self.camera_interfaces:
            camera.reset()

        # no delay is causing issues with the initial starting index
        self.poll_simulator()

        observation = self._observe()
        self.tracker.reset(start_idx=self.nearest_idx, segmentwise=segment_pos)

        # Evaluation mode
        # self.evaluate = evaluate

        return observation

    def poll_simulator(self):
        """Poll the simulator until it receives an action"""
        logging.info("Polling simulator...")
        logging.info("Validating driver configuration for polling...")

        for _ in range(500):
            self.action_interface.act(action=(1.0, 1.0))
            if abs(self.pose_interface.get_data()[0]) > 0.05:
                logging.info("Successful")
                return
            time.sleep(0.1)

        raise Exception("Failed to connect to simulator")

    def render(self):
        """Not implmeneted. By default, the simulator provides a graphical
        interface, but can also be run on a server.
        """
        return self.imgs

    def _observe(self) -> Dict[str, Union[np.array, Dict[str, np.array]]]:
        """Perform an observation action by getting the most recent data from
        the pose and camera interfaces. To prevent observating immediately
        after executing an action, we include a small delay prior to actually
        requesting data from the sensor interfaces. Position coordinates are
        converted to a local ENU coordinate system to be consistent with the
        racetrack maps.

        :return: a tuple of numpy arrays (pose_data, images) with shapes
          (30,) and (height, width, 3), respectively
        :rtype: tuple
        """
        time.sleep(self.observation_delay)
        pose = self.pose_interface.get_data()
        self.imgs = {c.camera_name: c.get_data() for c in self.camera_interfaces}

        yaw = pose[12]
        bp = pose[22:25]
        a = pose[6:9]

        # provide racetrack ID in the observation
        pose[2] = RACETRACKS[self.active_level]

        # convert to local coordinate system
        x, y, z = pose[16], pose[15], pose[17]
        enu_x, enu_y, enu_z = convert_ll_to_enu(
            center=[x, y, z], ref_point=self.racetrack.ref_point
        )
        pose[16], pose[15], pose[17] = enu_x, enu_y, enu_z

        self.nearest_idx = self.racetrack.nearest_idx(np.asarray([enu_x, enu_y]))
        self.tracker.update(self.nearest_idx, enu_x, enu_y, enu_z, yaw, a, bp)

        return {"pose": pose, "images": self.imgs}

    def _is_complete(self):
        """Determine if the episode is complete. Termination conditions include
        car out-of-bounds, 3-laps successfully complete, not-moving-timeout,
        and max timesteps reached
        """
        return self.tracker.is_complete()

    def _load_map(self):
        """Load racetrack into a Racetrack object"""
        logging.info("Loading track")
        self.racetrack = load_track(level=self.active_level)

        self.tracker = ProgressTracker(
            n_indices=self.racetrack.n_indices,
            obs_delay=self.observation_delay,
            inner_track=self.racetrack.inside_path,
            outer_track=self.racetrack.outside_path,
            centerline=self.racetrack.centerline_arr,
            car_dims=CAR_DIMS,
            n_segments=N_SEGMENTS,
            segment_idxs=self.racetrack.local_segment_idxs,
            segment_tree=self.racetrack.segment_tree,
            eval_mode=self.evaluate,
            coord_multiplier=COORD_MULTIPLIER[self.active_level],
        )

        self.reward.set_track(
            inside_path=self.racetrack.inside_path,
            outside_path=self.racetrack.outside_path,
            centre_path=self.racetrack.centre_path,
            car_dims=CAR_DIMS,
        )

    """ NOT USED CURRENTLY """

    def record_manually(
        self, output_dir, fname="thruxton", num_imgs=5000, sleep_time=0.03
    ):
        """Record observations, including images, to an output directory. This
        is useful for collecting images from the environment. This method does
        not use the an agent to take environment steps; instead, it just
        listens for observations while a user manually drives the car in the
        simulator.

        :param str output_dir: path of the output directory
        :param str fname: file name for output
        :param int num_imgs: number of images to record
        :param float sleep_time: time to sleep between images, in seconds
        """
        self.reset()

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        observations = []
        for i in range(num_imgs):
            observations.append(self._observe())
            time.sleep(sleep_time)

        for n, observation in enumerate(observations):
            pose, img = observation
            filename = f"{output_dir}/{fname}_{n}"
            np.savez_compressed(filename, pose_data=pose, image=img)

        print("Complete")

    def random_start_location(self):
        """Randomly selects an index on the centerline of the track and
        returns the ENU coordinates of the selected index along with the yaw of
        the centerline at that point.

        :returns: coordinates of a random index on centerline, yaw
        :rtype: np array, float
        """
        rand_idx = np.random.randint(0, len(self.racetrack.random_poses))
        pos = self.racetrack.random_poses[rand_idx]
        print(f"setting random location to: {pos}")
        coords = {"x": pos[0], "y": pos[1], "z": pos[2]}
        rot = {"yaw": pos[3], "pitch": 0.0, "roll": 0.0}
        return coords, rot

    def next_segment_start_location(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Get spawn location at beginning of next segement"""
        segment_idx = self.tracker.current_segment
        segment_idx = segment_idx % (N_SEGMENTS)

        pos = [0] * 4
        pos[0] = self.tracker.segment_coords["first"][segment_idx][0]  # x
        pos[1] = self.tracker.segment_coords["first"][segment_idx][1]  # y
        pos[2] = LEVEL_Z_DICT[self.active_level]  #
        pos[3] = self.racetrack.race_yaw[self.racetrack.local_segment_idxs[segment_idx]]

        coords = {"x": pos[0], "y": pos[1], "z": pos[2]}
        rot = {"yaw": pos[3], "pitch": 0.0, "roll": 0.0}

        self.tracker.current_segment += 1

        print("[Env] Spawning to next segment start location")
        print(f"[Env] Current segment: {self.tracker.current_segment}")
        print(
            "[Env] Respawns: {n_spawns}; infractions: {n_infr}".format(
                n_spawns=self.tracker.respawns, n_infr=self.tracker.num_infractions
            )
        )
        print(f"[Env] Coords: {coords}")
        print(f"[Env] Rot: {rot}")

        return coords, rot

    def _waypoints(self, goal="center", ct=3, step=8):
        """Return position of goal"""
        num = len(self.centerline_arr)
        idxs = [self.nearest_idx + i * step for i in range(ct)]
        if goal == "center":
            return np.asarray([self.centerline_arr[idx % num] for idx in idxs])
        else:
            raise NotImplementedError
