import csv
import deepdrive as deepdrive_capture
import deepdrive_client
import os
import queue
import random
import time
from collections import deque, OrderedDict
from multiprocessing import Process, Queue
from subprocess import Popen
import pkg_resources

import arrow
import gym
import numpy as np
from boto.s3.connection import S3Connection
from gym import spaces
from gym.utils import seeding

import config as c
import logs
import utils
from utils import obj2dict, download

log = logs.get_log(__name__)
SPEED_LIMIT_KPH = 64.

class Score(object):
    total = 0
    gforce_penalty = 0
    speed_reward = 0
    lane_deviation_penalty = 0
    progress_reward = 0
    got_stuck = False

    def __init__(self):
        self.start_time = time.time()
        self.end_time = None


class Action(object):
    def __init__(self, steering=0, throttle=0, brake=0, handbrake=0, has_control=True):
        self.steering = steering
        self.throttle = throttle
        self.brake = brake
        self.handbrake = handbrake
        self.has_control = has_control

    def as_gym(self):
        ret = gym_action(steering=self.steering, throttle=self.throttle, brake=self.brake,
                         handbrake=self.handbrake, has_control=self.has_control)
        return ret

    @classmethod
    def from_gym(cls, action):
        ret = cls(steering=action[0][0], throttle=action[1][0],
                  brake=action[2][0], handbrake=action[3][0], has_control=action[4])
        return ret


class Camera(object):
    def __init__(self, name, field_of_view, capture_width, capture_height, relative_position, relative_rotation):
        self.name = name
        self.field_of_view = field_of_view
        self.capture_width = capture_width
        self.capture_height = capture_height
        self.relative_position = relative_position
        self.relative_rotation = relative_rotation
        self.connection_id = None


default_cam = Camera(name='front_cam', field_of_view=60, capture_width=227, capture_height=227,
                     relative_position=[150, 1.0, 200],
                     relative_rotation=[0.0, 0.0, 0.0])

def gym_action(steering=0, throttle=0, brake=0, handbrake=0, has_control=True):
    action = [np.array([steering]),
              np.array([throttle]),
              np.array([brake]),
              np.array([handbrake]),
              has_control]
    return action

# noinspection PyMethodMayBeStatic
class DeepDriveEnv(gym.Env):
    metadata = {'render.modes': ['human']}

    def __init__(self, preprocess_with_tensorflow=False):
        self.action_space = self._init_action_space()
        self.preprocess_with_tensorflow = preprocess_with_tensorflow
        self.sess = None
        self.prev_observation = None
        self.start_time = time.time()
        self.step_num = 0
        self.prev_step_time = None
        self.display_stats = OrderedDict()
        self.display_stats['g-forces']                      = {'value': 0, 'ymin': 0,     'ymax': 3,  'units': ''}
        # self.display_stats['lane deviation']                = {'value': 0, 'ymin': 0,     'ymax': 1000,  'units': 'cm'}
        self.display_stats['gforce penalty']                = {'value': 0, 'ymin': 0,     'ymax': 5,   'units': ''}
        self.display_stats['lane deviation penalty']        = {'value': 0, 'ymin': 0,     'ymax': 40,   'units': ''}
        self.display_stats['speed reward']                  = {'value': 0, 'ymin': 0,     'ymax': 5,   'units': ''}
        self.display_stats['progress reward']               = {'value': 0, 'ymin': 0,     'ymax': 5,   'units': ''}
        self.display_stats['reward']                        = {'value': 0, 'ymin': -20,   'ymax': 20,    'units': ''}
        self.display_stats['score']                         = {'value': 0, 'ymin': -500,  'ymax': 10000, 'units': ''}
        self.dashboard_process = None
        self.dashboard_queue = None
        self.should_exit = False
        self.sim_process = None
        self.client_id = None
        self.has_control = None
        self.cameras = None
        self.use_sim_start_command = None
        self.connection_props = None

        if not c.REUSE_OPEN_SIM:
            if utils.get_sim_bin_path() is None:
                print('\n--------- Simulator not found, downloading ----------')
                if c.IS_LINUX or c.IS_WINDOWS:
                    url = c.BASE_URL + self.get_latest_sim_file()
                    download(url, c.SIM_PATH, warn_existing=False, overwrite=False)
                else:
                    raise NotImplementedError('Sim download not yet implemented for this OS')
            utils.ensure_executable(utils.get_sim_bin_path())

        self.client_version = pkg_resources.get_distribution("deepdrive").version
        # TODO: Check with connection version

        # collision detection  # TODO: Remove in favor of in-game detection
        self.reset_forward_progress()

        self.distance_along_route = 0
        self.start_distance_along_route = 0

        # reward
        self.score = Score()

        # laps
        self.lap_number = None
        self.prev_lap_score = 0
        self.should_end_on_lap = None

        # benchmarking - carries over across resets
        self.should_benchmark = False
        self.done_benchmarking = False
        self.trial_scores = []

    def open_sim(self):
        if c.REUSE_OPEN_SIM:
            return
        if self.use_sim_start_command:
            log.info('Starting simulator with command %s - this will take a few seconds.',
                     c.SIM_START_COMMAND)

            self.sim_process = Popen(c.SIM_START_COMMAND)

            import win32gui
            import win32process

            def get_hwnds_for_pid(pid):
                def callback(hwnd, _hwnds):
                    if win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd):
                        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                        if found_pid == pid:
                            _hwnds.append(hwnd)
                    return True

                hwnds = []
                win32gui.EnumWindows(callback, hwnds)
                return hwnds

            focused = False
            while not focused:
                time.sleep(1)
                dd_hwnds = get_hwnds_for_pid(self.sim_process.pid)
                if not dd_hwnds:
                    log.info('No windows found, waiting')
                else:
                    try:
                        win32gui.SetForegroundWindow(dd_hwnds[0])
                        focused = True
                    except:
                        log.info('Window not ready, waiting')

            pass
        else:
            log.info('Starting simulator at %s (takes a few seconds the first time).', utils.get_sim_bin_path())
            self.sim_process = Popen([utils.get_sim_bin_path()])

    def close_sim(self):
        if self.sim_process is not None:
            self.sim_process.kill()
            i = 0
            max_tries = 5
            while self.sim_process.poll() is None and i < max_tries:
                log.info('Waiting for sim to close')
                time.sleep(0.5)
                i += 1
            if i == max_tries:
                self.sim_process.terminate()



    def set_use_sim_start_command(self, use_sim_start_command):
        self.use_sim_start_command = use_sim_start_command

    @staticmethod
    def get_latest_sim_file():
        if c.IS_WINDOWS:
            os_name = 'windows'
        elif c.IS_LINUX:
            os_name = 'linux'
        else:
            raise RuntimeError('Unexpected OS')
        sim_prefix = 'sim/deepdrive-sim-'
        conn = S3Connection()
        bucket = conn.get_bucket('deepdrive')
        deepdrive_version = pkg_resources.get_distribution('deepdrive').version
        major_minor = deepdrive_version[:deepdrive_version.rindex('.')]
        sim_versions = list(bucket.list(sim_prefix + os_name + '-' + major_minor))

        latest_sim_file, path_version = sorted([(x.name, x.name.split('.')[-2])
                                           for x in sim_versions],
                                          key=lambda y: y[1])[-1]
        return '/' + latest_sim_file

    def init_benchmarking(self):
        self.should_benchmark = True
        os.makedirs(c.BENCHMARK_DIR, exist_ok=True)

    def start_dashboard(self):
        if utils.is_debugging():
            # TODO: Deal with plot UI not being in the main thread somehow - (move to browser?)
            log.warning('Dashboard not supported in debug mode')
            return
        q = Queue()
        p = Process(target=dashboard, args=(q,))
        p.start()
        self.dashboard_process = p
        self.dashboard_queue = q

    def set_tf_session(self, session):
        self.sess = session

    def set_to_end_on_lap(self, should_end_on_lap):
        self.should_end_on_lap = should_end_on_lap

    def _step(self, action):
        self.send_control(Action.from_gym(action))
        obz = self.get_observation()
        now = time.time()
        done = False
        reward = self.get_reward(obz, now)
        done = self.compute_lap_statistics(done, obz)
        self.prev_step_time = now

        if self.dashboard_queue is not None:
            self.dashboard_queue.put({'display_stats': self.display_stats, 'should_stop': False})
        if self.is_stuck(obz):  # TODO: derive this from collision, time elapsed, and distance as well
            done = True
            reward -= -10000  # reward is in scale of meters
        info = {}
        self.step_num += 1
        return obz, reward, done, info

    def compute_lap_statistics(self, done, obz):
        if not obz:
            return done
        lap_number = obz.get('lap_number')
        if lap_number is not None and self.lap_number is not None and self.lap_number < lap_number:
            lap_score = self.score.total - self.prev_lap_score
            log.info('lap %d complete with score of %f, speed reward', lap_number, lap_score)
            self.prev_lap_score = self.score.total
            if self.should_benchmark:
                self.log_benchmark_trial()
                if len(self.trial_scores) == 1000:
                    self.done_benchmarking = True
                done = True
            if self.should_end_on_lap:
                done = True
            self.log_up_time()
        self.lap_number = lap_number
        return done

    def get_reward(self, obz, now):
        reward = 0
        if obz:
            if self.prev_step_time is not None:
                time_passed = now - self.prev_step_time
            else:
                time_passed = None

            if time.time() - self.start_time < 2.5:
                # Give time to get on track after spawn
                reward = 0
            else:
                progress_reward = self.get_progress_reward(obz, time_passed)
                gforce_penalty = self.get_gforce_penalty(obz, time_passed)
                lane_deviation_penalty = self.get_lane_deviation_penalty(obz, time_passed)
                speed_reward = self.get_speed_reward(obz, time_passed)
                reward += (progress_reward + speed_reward - gforce_penalty - lane_deviation_penalty)

            self.score.total += reward
            self.display_stats['reward']['value'] = reward
            self.display_stats['score']['value'] = self.score.total

            log.debug('reward %r', reward)
            log.debug('score %r', self.score.total)

        return reward

    def log_up_time(self):
        log.info('up for %r' % arrow.get(time.time()).humanize(other=arrow.get(self.start_time), only_distance=True))

    def get_speed_reward(self, obz, time_passed):
        speed_reward = 0
        if 'speed' in obz:
            speed = obz['speed']
            if time_passed is not None:
                speed_reward = DeepDriveRewardCalculator.get_speed_reward(speed, time_passed)
                self.display_stats['speed reward']['value'] = speed_reward
        self.score.speed_reward += speed_reward
        return speed_reward

    def get_lane_deviation_penalty(self, obz, time_passed):
        lane_deviation_penalty = 0
        if 'distance_to_center_of_lane' in obz:
            lane_deviation_penalty = DeepDriveRewardCalculator.get_lane_deviation_penalty(
                obz['distance_to_center_of_lane'], time_passed)
        self.display_stats['lane deviation penalty']['value'] = lane_deviation_penalty
        self.score.lane_deviation_penalty += lane_deviation_penalty
        return lane_deviation_penalty

    def get_gforce_penalty(self, obz, time_passed):
        gforce_penalty = 0
        if 'acceleration' in obz:
            if time_passed is not None:
                a = obz['acceleration']
                gforces = np.sqrt(a.dot(a)) / 980  # g = 980 cm/s**2
                self.display_stats['g-forces']['value'] = gforces
                gforce_penalty = DeepDriveRewardCalculator.get_gforce_penalty(gforces, time_passed)

        self.display_stats['gforce penalty']['value'] = gforce_penalty
        self.score.gforce_penalty += gforce_penalty
        return gforce_penalty

    def get_progress_reward(self, obz, time_passed):
        progress_reward = 0
        if 'distance_along_route' in obz:
            dist = obz['distance_along_route'] - self.start_distance_along_route
            progress = dist - self.distance_along_route
            self.distance_along_route = dist
            DeepDriveRewardCalculator.get_progress_reward(progress, time_passed)
        self.display_stats['progress reward']['value'] = progress_reward
        self.score.progress_reward += progress_reward
        return progress_reward

    def is_stuck(self, obz):
        # TODO: Get this from the game instead

        if 'TEST_BENCHMARK_WRITE' in os.environ:
            self.score.got_stuck = True
            self.log_benchmark_trial()
            return True

        if obz is None:
            return False
        if obz['speed'] < 100:  # cm/s
            self.steps_crawling += 1
            if obz['throttle'] > 0:
                self.steps_crawling_with_throttle_on += 1
            if time.time() - self.last_forward_progress_time > 1 and \
                    (self.steps_crawling_with_throttle_on / float(self.steps_crawling)) > 0.8:
                self.reset_forward_progress()
                if self.should_benchmark:
                    self.score.got_stuck = True
                    self.log_benchmark_trial()
                return True
        else:
            self.reset_forward_progress()
        return False

    def log_benchmark_trial(self):
        self.score.end_time = time.time()
        self.trial_scores.append(self.score)
        totals = [s.total for s in self.trial_scores]
        median = np.median(totals)
        average = np.mean(totals)
        high = max(totals)
        low = min(totals)
        std = np.std(totals)
        log.info('benchmark lap #%d score: %f - high score: %f', len(self.trial_scores), self.score.total, median)
        filename = os.path.join(c.BENCHMARK_DIR, c.DATE_STR + '.csv')
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            for i, score in enumerate(self.trial_scores):
                if i == 0:
                    writer.writerow(['lap #', 'score', 'speed reward', 'progress reward', 'lane deviation penalty',
                                     'gforce penalty', 'got stuck', 'start', 'end', 'lap time'])
                writer.writerow([i + 1, score.total, score.speed_reward,
                                 score.progress_reward, score.lane_deviation_penalty,
                                 score.gforce_penalty, score.got_stuck, str(arrow.get(score.start_time).to('local')),
                                 str(arrow.get(score.end_time).to('local')),
                                 score.end_time - score.start_time])
            writer.writerow([])
            writer.writerow(['median score', median])
            writer.writerow(['avg score', average])
            writer.writerow(['std', std])
            writer.writerow(['high score', high])
            writer.writerow(['low score', low])
        log.info('wrote results to %r', filename)

    def release_agent_control(self):
        self.has_control = deepdrive_client.release_agent_control(self.client_id) is not None

    def request_agent_control(self):
        self.has_control = deepdrive_client.request_agent_control(self.client_id) == 1

    # noinspection PyAttributeOutsideInit
    def reset_forward_progress(self):
        self.last_forward_progress_time = time.time()
        self.steps_crawling_with_throttle_on = 0
        self.steps_crawling = 0

    def _reset(self):
        self.prev_observation = None
        # done = False
        # i = 0

        self.reset_agent()
        # while not done:
        #
        #     obz = self.get_observation()
        #     if obz and obz['distance_along_route'] < 20 * 100:
        #         self.start_distance_along_route = obz['distance_along_route']
        #         done = True
        #     time.sleep(1)
        #     i += 1
        #     if i > 5:
        #         log.error('Unable to reset, try restarting the sim.')
        #         raise Exception('Unable to reset, try restarting the sim.')
        self.send_control(Action(handbrake=True))
        self.step_num = 0
        self.distance_along_route = 0
        self.start_distance_along_route = 0
        self.prev_step_time = None
        self.lap_number = None
        self.prev_lap_score = 0
        self.score = Score()
        self.start_time = time.time()

    def change_viewpoint(self, cameras, use_sim_start_command):
        self.use_sim_start_command = use_sim_start_command
        deepdrive_capture.close()
        deepdrive_client.close(self.client_id)
        self.client_id = 0
        self.close_sim()  # Need to restart process now to change cameras
        self.open_sim()
        self.connect(cameras)

    def _close(self):
        if self.dashboard_queue is not None:
            self.dashboard_queue.put({'should_stop': True})
            self.dashboard_queue.close()
        if self.dashboard_process is not None:
            self.dashboard_process.join()
        deepdrive_capture.close()
        deepdrive_client.release_agent_control(self.client_id)
        deepdrive_client.close(self.client_id)
        self.client_id = 0
        if self.sess:
            self.sess.close()
        self.close_sim()

    def _render(self, mode='human', close=False):
        # TODO: Implement proper render - this is really only good for one frame - Could use our OpenGLUT viewer (on raw images) for this or PyGame on preprocessed images
        if self.prev_observation is not None:
            for camera in self.prev_observation['cameras']:
                utils.show_camera(camera['image'], camera['depth'])
        pass

    def _seed(self, seed=None):
        self.np_random = seeding.np_random(seed)
        # TODO: Generate random actions with this seed

    def preprocess_observation(self, observation):
        if observation:
            ret = obj2dict(observation, exclude=['cameras'])
            if observation.camera_count > 0 and getattr(observation, 'cameras', None) is not None:
                cameras = observation.cameras
                ret['cameras'] = self.preprocess_cameras(cameras)
            else:
                ret['cameras'] = []
        else:
            ret = None
        return ret

    def preprocess_cameras(self, cameras):
        ret = []
        for camera in cameras[:1]:  # Ignore all but the first camera  TODO: Don't hardcode this!
            image = camera.image_data.reshape(camera.capture_height, camera.capture_width, 3)
            depth = camera.depth_data.reshape(camera.capture_height, camera.capture_width)
            start_preprocess = time.time()
            if self.preprocess_with_tensorflow:
                import tf_utils  # avoid hard requirement on tensorflow
                if self.sess is None:
                    raise Exception('No tensorflow session. Did you call set_tf_session?')
                # This runs ~2x slower (18ms on a gtx 980) than CPU when we are not running a model due to
                # transfer overhead, but we do it anyway to keep training and testing as similar as possible.
                image = tf_utils.preprocess_image(image, self.sess)
                depth = tf_utils.preprocess_depth(depth, self.sess)
            else:
                image = utils.preprocess_image(image)
                depth = utils.preprocess_depth(depth)

            end_preprocess = time.time()
            log.debug('preprocess took %rms', (end_preprocess - start_preprocess) * 1000.)
            camera_out = obj2dict(camera, exclude=['image', 'depth'])
            camera_out['image'] = image
            camera_out['depth'] = depth
            ret.append(camera_out)
        return ret

    def get_observation(self):
        try:
            obz = deepdrive_capture.step()
        except SystemError as e:
            log.error('caught error during step' + str(e))
            ret = None
        else:
            ret = self.preprocess_observation(obz)
        log.debug('completed capture step')
        self.prev_observation = ret
        return ret

    def reset_agent(self):
        self.request_agent_control()
        deepdrive_client.reset_agent(self.client_id)

    def send_control(self, action):
        if self.has_control != action.has_control:
            self.change_has_control(action.has_control)
        deepdrive_client.set_control_values(self.client_id, steering=action.steering, throttle=action.throttle,
                                            brake=action.brake, handbrake=action.handbrake)

    def connect(self, cameras=None):
        def _connect():
            self.connection_props = deepdrive_client.create('127.0.0.1', 9876)
            if isinstance(self.connection_props, int):
                raise Exception('You have an old version of the deepdrive client - try uninstalling and reinstalling with pip')
            if not self.connection_props or not self.connection_props['max_capture_resolution']:
                # Try again
                return
            self.client_id = self.connection_props['client_id']
            server_version = self.connection_props['server_protocol_version']
            # TODO: Restore git commit timestamp version for release - this does hashing for free, once
            #   Then, for dev, store hash of .cpp and .h files on extension build inside VERSION_DEV, then when
            #   connecting, compute same hash and compare. (Need to figure out what to do on dev packaged version as
            #   files may change - maybe ignore as it's uncommon).
            #   Currently, we timestamp the build, and set that as the version in the extension. This is fine unless
            #   you change shared code and build the extension only, then the versions won't change, and you could
            #   see incompatibilities.
            if self.client_version != self.connection_props['server_protocol_version']:
                raise RuntimeError('Server and client version do not match - server is %s and client is %s' %
                                   (server_version, self.client_version))
        _connect()
        cxn_attempts = 0
        max_cxn_attempts = 10
        while not self.connection_props:
            cxn_attempts += 1
            sleep = cxn_attempts + random.random() * 2  # splay to avoid thundering herd
            log.warning('Connection to environment failed, retry (%d/%d) in %d seconds',
                        cxn_attempts, max_cxn_attempts, round(sleep, 0))
            time.sleep(sleep)
            _connect()
            if cxn_attempts >= max_cxn_attempts:
                raise RuntimeError('Could not connect to the environment')

            if cameras is None:
                cameras = [default_cam]
        self.cameras = cameras
        if self.client_id and self.client_id > 0:
            for cam in self.cameras:
                cam['cxn_id'] = deepdrive_client.register_camera(self.client_id, cam['field_of_view'],
                                                              cam['capture_width'],
                                                              cam['capture_height'],
                                                              cam['relative_position'],
                                                              cam['relative_rotation'])

            shared_mem = deepdrive_client.get_shared_memory(self.client_id)
            self.reset_capture(shared_mem[0], shared_mem[1])
            self._init_observation_space()
        else:
            self.raise_connect_fail()
        self.has_control = False

    def reset_capture(self, shared_mem_name, shared_mem_size):
        n = 10
        sleep = 0.1
        log.debug('Connecting to deepdrive...')
        while n > 0:
            # TODO: Establish some handshake so we don't hardcode size here and in Unreal project
            if deepdrive_capture.reset(shared_mem_name, shared_mem_size):
                log.debug('Connected to deepdrive shared capture memory')
                return
            n -= 1
            sleep *= 2
            log.debug('Sleeping %r', sleep)
            time.sleep(sleep)
        log.error('Could not connect to deepdrive capture memory at %s', shared_mem_name)
        self.raise_connect_fail()

    @staticmethod
    def raise_connect_fail():
        if c.SIM_START_COMMAND:
            raise Exception('Could not connect to environment. You may need to close the Unreal Editor and/or turn off '
                            'saving CPU in background in the Editor preferences (search for CPU).')
        else:
            raise Exception('\n\n\n'
                        '**********************************************************************\n'
                        '**********************************************************************\n'
                        '****                                                              ****\n\n'
                        '|               Could not connect to the environment.                |\n\n'
                        '****                                                              ****\n'
                        '**********************************************************************\n'
                        '**********************************************************************\n\n')

    def _init_action_space(self):
        steering_space = spaces.Box(low=-1, high=1, shape=1)
        throttle_space = spaces.Box(low=-1, high=1, shape=1)
        brake_space = spaces.Box(low=0, high=1, shape=1)
        handbrake_space = spaces.Box(low=0, high=1, shape=1)
        is_game_driving_space = spaces.Discrete(2)
        action_space = spaces.Tuple(
            (steering_space, throttle_space, brake_space, handbrake_space, is_game_driving_space))
        return action_space

    def _init_observation_space(self):
        obz_spaces = []
        for camera in self.cameras:
            obz_spaces.append(spaces.Box(low=0, high=255, shape=(camera['capture_width'], camera['capture_height'])))
        observation_space = spaces.Tuple(tuple(obz_spaces))
        self.observation_space = observation_space
        return observation_space

    def change_has_control(self, has_control):
        if has_control:
            self.request_agent_control()
        else:
            self.release_agent_control()


class DeepDriveRewardCalculator(object):
    @staticmethod
    def clip(reward):
        # time_passed not parameter in order to set hard limits on reward magnitude
        return min(max(reward, -1e2), 1e2)

    @staticmethod
    def get_speed_reward(cmps, time_passed):
        """
        Incentivize going quickly while remaining under the speed limit.
        :param cmps: speed in cm / s
        :param time_passed: time passed since previous speed reward (allows running at variable frame rates while
        still receiving consistent rewards)
        :return: positive or negative real valued speed reward on meter scale
        """
        speed_kph = cmps * 3600. / 100. / 1000.  # cm/s=>kph
        balance_coeff = 2. / 10.
        speed_delta = speed_kph - SPEED_LIMIT_KPH
        if speed_delta > 4:
            # too fast
            speed_reward = -1 * balance_coeff * speed_kph * time_passed * speed_delta ** 2  # squared to outweigh advantage of speeding
        else:
            # incentivize timeliness
            speed_reward = balance_coeff * time_passed * speed_kph
        # No slow penalty as progress already incentivizes this (plus we'll need to stop at some points anyway)

        speed_reward = DeepDriveRewardCalculator.clip(speed_reward)
        return speed_reward

    @staticmethod
    def get_lane_deviation_penalty(lane_deviation, time_passed):
        lane_deviation_penalty = 0
        if lane_deviation < 0:
            raise ValueError('Lane deviation should be positive')
        if time_passed is not None and lane_deviation > 200:  # Tuned for Canyons spline - change for future maps
            lane_deviation_coeff = 0.1
            lane_deviation_penalty = lane_deviation_coeff * time_passed * lane_deviation ** 2 / 100.
        # self.display_stats['lane deviation']['value'] = lane_deviation
        log.debug('distance_to_center_of_lane %r', lane_deviation)
        lane_deviation_penalty = DeepDriveRewardCalculator.clip(lane_deviation_penalty)
        return lane_deviation_penalty

    @staticmethod
    def get_gforce_penalty(gforces, time_passed):
        log.debug('gforces %r', gforces)
        gforce_penalty = 0
        if gforces < 0:
            raise ValueError('G-Force should be positive')
        if gforces > 0.5:
            # https://www.quora.com/Hyperloop-What-is-a-physically-comfortable-rate-of-acceleration-for-human-beings
            time_weighted_gs = time_passed * gforces
            time_weighted_gs = min(time_weighted_gs,
                                   5)  # Don't allow a large frame skip to ruin the approximation
            balance_coeff = 24  # 24 meters of reward every second you do this
            gforce_penalty = time_weighted_gs * balance_coeff
            log.debug('accumulated_gforce %r', time_weighted_gs)
            log.debug('gforce_penalty %r', gforce_penalty)
        gforce_penalty = DeepDriveRewardCalculator.clip(gforce_penalty)
        return gforce_penalty

    @staticmethod
    def get_progress_reward(progress, time_passed):
        if time_passed is not None:
            step_velocity = progress / time_passed
            if step_velocity < -400 * 100:
                # Lap completed
                # TODO: Read the lap length on reset and
                log.info('assuming lap complete')
                progress = 0
        progress_reward = progress / 100.  # cm=>meters
        balance_coeff = 1.0
        progress_reward *= balance_coeff
        progress_reward = DeepDriveRewardCalculator.clip(progress_reward)
        return progress_reward


def dashboard(dash_queue):
    import matplotlib.animation as animation
    import matplotlib
    try:
        # noinspection PyUnresolvedReferences
        import matplotlib.pyplot as plt
    except ImportError as e:
        log.error('\n\n\n***** Error: Could not start dashboard: %s\n\n', e)
        return
    plt.figure(0)

    class Disp(object):
        stats = {}
        txt_values = {}
        lines = {}
        x_lists = {}
        y_lists = {}

    def get_next(block=False):
        try:
            q_next = dash_queue.get(block=block)
            if q_next['should_stop']:
                print('Stopping dashboard')
                try:
                    anim._fig.canvas._tkcanvas.master.quit()  # Hack to avoid "Exiting Abnormally"
                finally:
                    exit()
            else:
                Disp.stats = q_next['display_stats']
        except queue.Empty:
            # Reuuse old stats
            pass

    get_next(block=True)

    font = {'size': 8}

    matplotlib.rc('font', **font)

    for i, (stat_name, stat) in enumerate(Disp.stats.items()):
        stat = Disp.stats[stat_name]
        stat_label_subplot = plt.subplot2grid((len(Disp.stats), 3), (i, 0))
        stat_value_subplot = plt.subplot2grid((len(Disp.stats), 3), (i, 1))
        stat_graph_subplot = plt.subplot2grid((len(Disp.stats), 3), (i, 2))
        stat_label_subplot.text(0.5, 0.5, stat_name, fontsize=12, va="center", ha="center")
        txt_value = stat_value_subplot.text(0.5, 0.5, '', fontsize=12, va="center", ha="center")
        Disp.txt_values[stat_name] = txt_value
        stat_graph_subplot.set_xlim([0, 200])
        stat_graph_subplot.set_ylim([stat['ymin'], stat['ymax']])
        Disp.lines[stat_name], = stat_graph_subplot.plot([], [])
        stat_label_subplot.axis('off')
        stat_value_subplot.axis('off')
        Disp.x_lists[stat_name] = deque(np.linspace(200, 0, num=400))
        Disp.y_lists[stat_name] = deque([-1] * 400)
        frame1 = plt.gca()
        frame1.axes.get_xaxis().set_visible(False)

    plt.subplots_adjust(hspace=0.88)
    fig = plt.gcf()
    fig.set_size_inches(5.5, len(Disp.stats) * 0.5)
    fig.canvas.set_window_title('Dashboard')
    anim = None

    def init():
        lines = []
        for s_name in Disp.stats:
            line = Disp.lines[s_name]
            line.set_data([], [])
            lines.append(line)
        return lines

    def animate(_i):
        lines = []
        get_next()
        for s_name in Disp.stats:
            s = Disp.stats[s_name]
            xs = Disp.x_lists[s_name]
            ys = Disp.y_lists[s_name]
            tv = Disp.txt_values[s_name]
            line = Disp.lines[s_name]
            val = s['value']
            tv.set_text(str(round(val, 2)) + s['units'])
            ys.pop()
            ys.appendleft(val)
            line.set_data(xs, ys)
            lines.append(line)
        plt.draw()
        return lines

    # TODO: Add blit=True and deal with updating the text if performance becomes unacceptable
    anim = animation.FuncAnimation(fig, animate, init_func=init, frames=200, interval=100)
    plt.show()

if __name__ == '__main__':
    DeepDriveEnv.get_latest_sim_file()