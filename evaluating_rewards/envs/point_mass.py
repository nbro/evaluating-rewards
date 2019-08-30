# Copyright 2019 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A simple point-mass environment in N-dimensions."""

import functools

from evaluating_rewards import rewards
import gym
from imitation.envs import resettable_env
from imitation.util import registry
import numpy as np
from stable_baselines.common import policies
from stable_baselines.common import vec_env
import tensorflow as tf


class PointMassEnv(resettable_env.ResettableEnv):
  """A simple point-mass environment."""

  def __init__(self, ndim: int = 2, dt: float = 1e-1,
               ctrl_coef: float = 1.0, threshold: float = 0.05):
    """Builds a PointMass environment.

    Args:
      ndim: Number of dimensions.
      dt: Size of timestep.
      ctrl_coef: Weight for control cost.
      threshold: Distance to goal within which episode terminates.
          (Set negative to disable episode termination.)
    """
    super().__init__()

    self.ndim = ndim
    self.dt = dt
    self.ctrl_coef = ctrl_coef
    self.threshold = threshold

    substate_space = gym.spaces.Box(-np.inf, np.inf, shape=(ndim,))
    subspaces = {k: substate_space for k in ["pos", "vel", "goal"]}
    self._state_space = gym.spaces.Dict(spaces=subspaces)
    self._observation_space = gym.spaces.Box(-np.inf, np.inf, shape=(3 * ndim,))
    self._action_space = gym.spaces.Box(-1, 1, shape=(ndim,))

    self.viewer = None
    self._agent_transform = None
    self._goal_transform = None

  def initial_state(self):
    """Choose initial state randomly from region at least 1-step from goal."""
    while True:
      pos = self.rand_state.randn(self.ndim)
      vel = self.rand_state.randn(self.ndim)
      goal = self.rand_state.randn(self.ndim)
      dist = np.linalg.norm(pos - goal)
      min_dist_next = dist - self.dt * np.linalg.norm(vel)
      if min_dist_next > self.threshold:
        break
    return {"pos": pos, "vel": vel, "goal": goal}

  def transition(self, old_state, action):
    action = np.array(action)
    action = action.clip(-1, 1)
    return {
        "pos": old_state["pos"] + self.dt * old_state["vel"],
        "vel": old_state["vel"] + self.dt * action,
        "goal": old_state["goal"],
    }

  def reward(self, old_state, action, new_state):
    dist = np.linalg.norm(new_state["pos"] - new_state["goal"])
    ctrl_penalty = np.dot(action, action)
    return -dist - self.ctrl_coef * ctrl_penalty

  def terminal(self, state, step: int) -> bool:
    dist = np.linalg.norm(state["pos"] - state["goal"])
    return dist < self.threshold

  def obs_from_state(self, state):
    return np.concatenate([state["pos"], state["vel"], state["goal"]], axis=-1)

  def state_from_obs(self, obs):
    return {
        "pos": obs[:, 0:self.ndim],
        "vel": obs[:, self.ndim:2*self.ndim],
        "goal": obs[:, 2*self.ndim:3*self.ndim],
    }

  def render(self, mode="human"):
    if self.viewer is None:
      from gym.envs.classic_control import rendering  # pylint:disable=g-import-not-at-top
      self.viewer = rendering.Viewer(500, 500)
      self.viewer.set_bounds(-5, 5, -5, 5)

      def make_circle(**kwargs):
        obj = rendering.make_circle(**kwargs)
        transform = rendering.Transform()
        obj.add_attr(transform)
        self.viewer.add_geom(obj)
        return obj, transform

      goal, self._goal_transform = make_circle(radius=.2)
      goal.set_color(1.0, 0.85, 0.0)  # golden
      _, self._agent_transform = make_circle(radius=.1)

    def project(arr):
      if self.ndim == 1:
        assert len(arr) == 1
        return (arr[0], 0)
      elif self.ndim == 2:
        assert len(arr) == 2
        return tuple(arr)
      else:
        raise ValueError()

    self._goal_transform.set_translation(*project(self.cur_state["goal"]))
    self._agent_transform.set_translation(*project(self.cur_state["pos"]))

    return self.viewer.render(return_rgb_array=(mode == "rgb_array"))

  def close(self):
    if self.viewer:
      self.viewer.close()
      self.viewer = None


class PointMassGroundTruth(rewards.BasicRewardModel):
  """RewardModel representing the true (dense) reward in PointMass."""

  def __init__(self, env, ctrl_coef=1.0):
    self.ndim, remainder = divmod(env.observation_space.shape[0], 3)
    assert remainder == 0
    self.ctrl_coef = ctrl_coef
    super().__init__(env.observation_space, env.action_space)

    self._reward = self.build_reward()

  def build_reward(self):
    """Computes reward from observation and action in PointMass environment."""
    pos = self._proc_old_obs[:, 0:self.ndim]
    goal = self._proc_old_obs[:, 2*self.ndim:3*self.ndim]
    dist = tf.norm(pos - goal, axis=-1)
    ctrl_cost = tf.reduce_sum(tf.square(self._proc_act), axis=-1)
    return -dist - self.ctrl_coef * ctrl_cost

  @property
  def reward(self):
    """Reward tensor."""
    return self._reward


class PointMassSparseReward(rewards.BasicRewardModel):
  """A sparse reward for the point mass being close to the goal.

  Should produce similar behavior to PointMassGroundTruth. However, it is not
  equivalent up to potential shaping.
  """

  def __init__(self, env, ctrl_coef=1.0, threshold=0.05):
    self.ndim, remainder = divmod(env.observation_space.shape[0], 3)
    assert remainder == 0
    self.ctrl_coef = ctrl_coef
    self.threshold = threshold
    super().__init__(env.observation_space, env.action_space)

    self._reward = self.build_reward()

  def build_reward(self):
    """Computes reward from observation and action in PointMass environment."""
    pos = self._proc_old_obs[:, 0:self.ndim]
    goal = self._proc_old_obs[:, 2*self.ndim:3*self.ndim]
    dist = tf.norm(pos - goal, axis=-1)
    goal_reward = tf.to_float(dist < self.threshold)
    ctrl_cost = tf.reduce_sum(tf.square(self._proc_act), axis=-1)
    return goal_reward - self.ctrl_coef * ctrl_cost

  @property
  def reward(self):
    """Reward tensor."""
    return self._reward


class PointMassShaping(rewards.BasicRewardModel):
  """Potential shaping term, based on distance to goal."""

  def __init__(self, env):
    self.ndim, remainder = divmod(env.observation_space.shape[0], 3)
    assert remainder == 0
    super().__init__(env.observation_space, env.action_space)

    self._reward = self.build_reward()

  def build_reward(self):
    """Computes shaping from old and next observations."""
    def dist(obs):
      pos = obs[:, 0:self.ndim]
      goal = obs[:, 2*self.ndim:3*self.ndim]
      return tf.norm(pos - goal, axis=-1)

    old_dist = dist(self._proc_old_obs)
    new_dist = dist(self._proc_new_obs)

    return old_dist - new_dist

  @property
  def reward(self):
    """Reward tensor."""
    return self._reward


class PointMassPolicy(policies.BasePolicy):
  """Hard-coded policy that accelerates towards goal."""

  def __init__(self, env, magnitude=1.0):
    self.ob_space = env.observation_space
    self.ac_space = env.action_space
    self.ndim, remainder = divmod(env.observation_space.shape[0], 3)
    assert remainder == 0
    self.magnitude = magnitude

  def step(self, obs, state=None, mask=None, deterministic=False):
    pos = obs[:, 0:self.ndim]
    vel = obs[:, self.ndim:2 * self.ndim]
    goal = obs[:, 2 * self.ndim:3 * self.ndim]
    target_vel = goal - pos
    target_vel = target_vel / np.linalg.norm(target_vel, axis=1)
    delta_vel = target_vel - vel
    delta_vel_norm = np.linalg.norm(delta_vel, ord=np.inf, axis=1)
    act = delta_vel / max(delta_vel_norm, 1e-4)
    act = act.clip(-1, 1)
    return act, None, None, None

  def proba_step(self, obs, state=None, mask=None):
    raise NotImplementedError()


# Loaders for deserialize interface
load_point_mass_policy = registry.build_loader_fn_require_env(PointMassPolicy)
load_point_mass_ground_truth = registry.build_loader_fn_require_env(
    PointMassGroundTruth)
load_point_mass_sparse_reward = registry.build_loader_fn_require_env(
    PointMassSparseReward)
load_point_mass_sparse_reward_no_ctrl = registry.build_loader_fn_require_env(
    functools.partial(PointMassSparseReward, ctrl_coef=0.0))


def load_point_mass_dense_reward(path: str,  # pylint: disable=unused-argument
                                 venv: vec_env.VecEnv, **kwargs):
  return rewards.LinearCombinationModelWrapper({
      "sparse": (PointMassSparseReward(venv, **kwargs), tf.constant(1.0)),
      "shaping": (PointMassShaping(venv), tf.constant(10.0)),
  })


load_point_mass_dense_reward_no_ctrl = functools.partial(
    load_point_mass_dense_reward,
    ctrl_coef=0.0)