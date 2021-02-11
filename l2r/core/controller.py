# ========================================================================= #
# Filename:                                                                 #
#    controller.py                                                          #
#                                                                           #
# Description:                                                              # 
#    Contains the SimulatorController class to interface with the           #
#    simulator's via the API.                                               #
# ========================================================================= #
import json
import os
import time
from subprocess import Popen

from websocket import create_connection

from racetracks.mapping import level_2_simlevel

SHORT_DELAY=0.02
MEDIUM_DELAY=10

# Shell commands to launch and kill container
START_CMD = 'docker run -t --rm ' \
            '--user=ubuntu --gpus all ' \
            '--entrypoint="./ArrivalSim.sh" ' \
            '--net=host '

KILL_CMD = 'docker kill '

class SimulatorController(object):
	"""This class is used to communicate with the simulator which is
	accessible via Websocket. The methods in this class allow one to change
	the position of the vehicle, set the map, reset the map, change the
	settings (such as turning the camera on), etc.

	:param ip: IP address of the simulator
	:type ip: str, optional
	:param port: port to connect to
	:type port: str, optional
	:param quiet: flag to show print statements about changes being made to the
	  simulator
	:type quiet: boolean, optional
	:param sim_version: version of the simulator
	:type sim_version: str, optional
	:param start_container: start simulator container. Assumes unix-based os
	:type start_container: boolean, optional
	:param image_name: name of the simulator's docker image
	:type image_name: str, optional
	"""
	def __init__(self, ip='0.0.0.0', port='16000', quiet=False,
		         sim_version='ArrivalSim-linux-0.3.0.137341-roborace',
		         start_container=True, image_name='arrival-sim',
		         container_name='racing-sim'):
		"""Constructor method
		"""
		self.start_container = start_container
		self.start = START_CMD + f'--name {container_name} {image_name}' 
		self.kill = KILL_CMD + container_name
		
		if start_container:
			_ = self.start_simulator()

		self.addr = f'{ip}:{port}'
		self.ws = create_connection(f'ws://{self.addr}')
		self.sim_version = sim_version
		self.drive_mode_param = ('bIsManulMode'
			                     if sim_version.startswith('RoboraceMaps')
			                     else 'bADModeInput')
		self.id = 0
		self.quiet = quiet

	def __del__(self):
		"""Stop the simulator on exit
		"""
		if self.start_container:
			print('Controller destroyed. Tearing down simulator.')
			self.kill_simulator()

	def start_simulator(self):
		"""Starts the simulator container
		"""
		with open('/tmp/sim_log.txt', 'w') as out:
			Popen(self.start, shell=True, stdout=out, stderr=out)

		time.sleep(MEDIUM_DELAY)
		return

	def kill_simulator(self):
		"""Kill the simulator container
		"""
		Popen(self.kill, shell=True)
		time.sleep(MEDIUM_DELAY)

	def restart_simulator(self):
		"""Restarts the simulator container
		"""
		self.kill_simulator()
		self.start_simulator()
		self._print('Reconnecting', force=True)
		self.ws = create_connection(f'ws://{self.addr}')

	def set_level(self, level):
		"""Sets the simulator to a specified level (map).

		:param level: name of the racetrack. must be in ['VegasNorthRoad', 
		  'Thruxton']
		:type level: string 
		"""
		level_name = level_2_simlevel(level, self.sim_version)
		self._print(f'Setting level to {level}')
		return self._send_msg(
					method='open_level',
					params={'level_name':level_name}
				)

	def set_location(self, coords, veh_id=0):
		"""Sets a vehicle to a specific location on track.

		:param coords: desired ENU coordinates
		:type coords: numpy array or list of floats [East, North, Up]
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		e, n, u = coords[0], coords[1], coords[2]
		return self._send_msg(
			method='set_vehicle_inputs',
			params={'veh_id': veh_id,
					'pos_xyz': [e, n, u],
					}
		)

	def reset_level(self):
		"""Resets the map to its default configuration. This does not reset
		the vehicle parameters, but it does set the vehicle to it's default
		pose.
		"""
		self._print(f'Resetting level')
		_ = self._send_msg(method='reset_level')

	def get_level(self):
		"""Get the active level of the simulator.
		
		:return: name of the simulator's active racetrack
		:rtype: str
		"""
		return self._send_msg(method='get_level')

	def get_levels(self):
		"""Gets the levels available in the simulator.

		:return: list of the levels available in the simulator
		:rtype: list of str
		"""
		return self._send_msg(method='get_levels')

	def get_position(self, veh_id=0):
		"""Get vehicle's current position.

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: ENU coordinates of the vehicle [East, North, Up]
		:rtype: list of floats
		"""
		result = self._send_msg(
			method='get_vehicle_state',
			params={'veh_id': veh_id}
		)
		return result['pos_xyz']

	def get_vehicle_params(self, veh_id=0):
		"""Get the active parameters of a vehicle.

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: a dictionary of the vehicle's parameters
		:rtype: dict
		"""
		return self._send_msg(
			method='get_vehicle_params',
			params={'veh_id': veh_id}
		)

	def set_vehicle_params(self, parameters, veh_id=0):
		"""Set parameters of a vehicle.

		:param parameters: name value pairs
		:type parameters: list of dictionaries
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		self._print(f'Setting vehicle parameters for vehicle: {veh_id}')
		_ = self._send_msg(
			method='set_vehicle_params',
			params={
				'veh_id': veh_id,
				'parameters': parameters
			}
		)

	def reset_vehicle_params(self, veh_id=0):
		"""Reset a vehicle's parameters to default.
		
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		self._print(f'Resetting vehicle parameters for vehicle: {veh_id}')
		_ = self._send_msg(
			method='reset_default_vehicle_params',
			params={'veh_id': veh_id}
		)

	def get_active_vehicles(self):
		"""Get a list of the active vehicles in the simulator.

		:return: list of active vehicles in the following format:
		  {"veh_id": <veh_id>, "vehicle_class_name": <vehicle_class_name>}
		:rtype: list of dictionaries
		"""
		return self._send_msg(
			method='get_vehicle_classes'
		)

	def get_vehicle_classes(self):
		"""Get a list of the vehicle classes.

		:return: list of the vehicle classes
		:rtype: list
		"""
		return self._send_msg(
			method='get_vehicle_classes'
		)

	def get_sensors_params(self, veh_id=0):
		"""Get the parameters of each sensor for a specified vehicle.

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: list of parameters for each sensor
		:rtype: list
		"""
		return self._send_msg(
			method='get_sensors_params',
			params={'veh_id': veh_id}
		)

	def get_sensor_params(self, sensor, veh_id=0):
		"""Get the parameters of a specified sensor of a specified vehicle.

		:param sensor: name of sensor to get the params for
		:type sensor: str
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: the sensor's parameters
		:rtype: dict
		"""
		return self._send_msg(
			method='get_sensor_params',
			params={
				'veh_id': veh_id,
				'sensor_name': sensor
			}
		)

	def set_sensor_param(self, sensor, name, value, veh_id=0):
		"""Set a specified parameter to a specified value of a specified
		sensor on a specified vehicle.

		:param sensor: name of sensor to set
		:type sensor: str
		:param name: name of the parameter to set
		:type name: str
		:param value: value of the parameter to set
		:type value: str
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: 0, if successful
		:rtype: int
		"""
		self._print(f'Setting: {name} to: {value} for vehicle: {veh_id}')
		return self._send_msg(
			method='set_sensor_param',
			params={
				'veh_id': veh_id,
				'sensor_name': sensor,
				'parameter_name': name,
				'value': value
			}
		)	

	def set_sensor_params(self, sensor, params, veh_id=0):
		"""Set specified parameters of a specified sensor on a specified
		vehicle.

		:param sensor: name of sensor to set
		:type sensor: str
		:param parameters: name value pairs of the parameters to set
		:type parameters: dictionary
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		self._print(f'Setting: {sensor} parameters for vehicle: {veh_id}')
		parameters = [{'name': k, 'value': v} for k, v in params.items()]
		_ = self._send_msg(
			method='set_sensor_params',
			params={
				'veh_id': veh_id,
				'sensor_name': sensor,
				'parameters': parameters
			}
		)

	def get_vehicle_driver_params(self, veh_id=0):
		"""Get the parameters of the sensor 'ArrivalVehicleDriver'.

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: parameters of the vehicle driver
		:rtype: dict
		"""
		return self._send_msg(
			method='get_sensor_params',
			params={
				'veh_id': veh_id,
				'sensor_name': 'ArrivalVehicleDriver'
			}
		)

	def get_driver_mode(self, veh_id=0):
		"""Get the mode of a specified vehicle.

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: true if manual mode, false if AI mode
		:rtype: boolean
		"""
		return self._send_msg(
			method='get_sensor_param',
			params={
				'veh_id': veh_id,
				'sensor_name': 'ArrivalVehicleDriver',
				'parameter_name': self.drive_mode_param
			}
		)

	def set_mode_ai(self, veh_id=0):
		"""Sets a specified vehicle mode to "AI".

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		self._print(f'Setting to AI mode for vehicle: {veh_id}')
		_ = self._send_msg(
			method='set_sensor_param',
			params={
				'veh_id': veh_id,
				'sensor_name': 'ArrivalVehicleDriver',
				'parameter_name': self.drive_mode_param,
				'value': True
			}
		)

	def set_mode_manual(self, veh_id=0):
		"""Sets a specified vehicle mode to "manual".

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		self._print(f'Setting to manual mode for vehicle: {veh_id}')
		_ = self._send_msg(
			method='set_sensor_param',
			params={
				'veh_id': veh_id,
				'sensor_name': 'ArrivalVehicleDriver',
				'parameter_name': self.drive_mode_param,
				'value': False
			}
		)

	def set_api_udp(self, veh_id=0):
		"""Set a specified vehicle API class to VApiUdp for to allow the RL
		agent to control it.

		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		"""
		self._print(f'Setting to RL mode for vehicle: {veh_id}')
		_ = self._send_msg(
			method='set_sensor_param',
			params={
				'veh_id': veh_id,
				'sensor_name': 'ArrivalVehicleDriver',
				'parameter_name': 'DriverAPIClass',
				'value': 'VApiUdp'
			}
		)
		
	def enable_sensor(self, sensor_name, veh_id=0):
		"""Activates a specified sensor on a specified vehicle.

		:param sensor_name: sensor to be activated
		:type sensor_name: str
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: 0, if successful
		:rtype: int
		"""
		self._print(f'Enabling {sensor_name}')
		return self._send_msg(
			method='enable_sensor',
			params={
				'veh_id': veh_id,
				'sensor_name': sensor_name
			}
		)

	def disable_sensor(self, sensor_name, veh_id=0):
		"""Disables a specified sensor on a specified vehicle.

		:param sensor_name: sensor to be deactivated
		:type sensor_name: str
		:param veh_id: identifier of the vehicle, defaults to 0
		:type veh_id: int, optional
		:return: 0, if successful
		:rtype: int
		"""
		self._print(f'Disabling {sensor_name}')
		return self._send_msg(
			method='disable_sensor',
			params={
				'veh_id': veh_id,
				'sensor_name': sensor_name
			}
		)

	def _send_msg(self, method, params=None):
		"""Helper routine to send jsonprc messages.

		:param method: specified method
		:type method: str
		:param params: parameters to send
		:type params: dict, optional
		:return: response from the simulator
		:rtype: varies
		"""
		self.id += 1
		msg = {"jsonrpc": "2.0", "params": {}, "id": self.id}
		msg['method'] = method
		if params:
			msg['params'] = params
		self.ws.send(json.dumps(msg))
		msg = json.loads(self.ws.recv())
		time.sleep(SHORT_DELAY)
		return msg['result']

	def _print(self, msg, force=False):
		"""Helper print routine
		"""
		if not self.quiet and not force:
			print(f'[SimulatorController] {msg}')