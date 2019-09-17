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

"""Common training boilerplate shared between CLI scripts."""

import os
from typing import Callable, TypeVar

from evaluating_rewards import rewards
from evaluating_rewards import serialize
import gym
from imitation import util
from stable_baselines.common import vec_env
import tensorflow as tf


T = TypeVar("T")
V = TypeVar("V")

EnvRewardFactory = Callable[[gym.Space, gym.Space],
                            rewards.RewardModel]


DEFAULT_CONFIG = {
    "env_name": "evaluating_rewards/PointMassLineFixedHorizon-v0",
    "target_reward_type": "evaluating_rewards/Zero-v0",
    "target_reward_path": "dummy",
    "model_reward_type": rewards.MLPRewardModel,
}


def logging_config(log_root, env_name):
  # pylint:disable=unused-variable
  log_dir = os.path.join(log_root, env_name.replace("/", "_"),
                         util.make_unique_timestamp())
  # pylint:enable=unused-variable


MakeTrainerFn = Callable[[rewards.RewardModel, tf.VariableScope,
                          rewards.RewardModel], T]
DoTrainingFn = Callable[[rewards.RewardModel, T], V]


def regress(seed: int,
            venv: vec_env.VecEnv,
            make_trainer: MakeTrainerFn,
            do_training: DoTrainingFn,

            target_reward_type: str,
            target_reward_path: str,

            model_reward_type: EnvRewardFactory,

            log_dir: str,
           ) -> V:
  """Train a model on target and save the results, reporting training stats."""
  with util.make_session() as (_, sess):
    tf.random.set_random_seed(seed)

    with tf.variable_scope("source") as model_scope:
      model = model_reward_type(venv.observation_space, venv.action_space)

    with tf.variable_scope("target"):
      target = serialize.load_reward(target_reward_type,
                                     target_reward_path, venv)

    with tf.variable_scope("train") as train_scope:
      trainer = make_trainer(model, model_scope, target)

    # Do not initialize any variables from target, which have already been
    # set during serialization.
    init_vars = model_scope.global_variables() + train_scope.global_variables()
    sess.run(tf.initializers.variables(init_vars))

    stats = do_training(target, trainer)

    model.save(os.path.join(log_dir, "model"))

  return stats