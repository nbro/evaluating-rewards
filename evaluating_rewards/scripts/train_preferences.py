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

"""CLI script to fit a model to synthetically generated preferences."""

import os
from typing import Any, Mapping

from absl import app
from evaluating_rewards import preferences
from evaluating_rewards import rewards
from evaluating_rewards.scripts import regress_utils
from evaluating_rewards.scripts import script_utils
from imitation.policies import serialize as policies_serialize
from imitation.util import util
import sacred


train_preferences_ex = sacred.Experiment("train_preferences")


# pylint:disable=unused-variable
@train_preferences_ex.config
def default_config():
  """Default configuration values."""
  locals().update(**regress_utils.DEFAULT_CONFIG)
  num_vec = 8  # number of environments in VecEnv

  policy_type = "random"  # type of policy to generate comparison trajectories
  policy_path = "dummy"  # path to policy

  # Hyperparameters
  model_reward_type = rewards.MLPRewardModel
  total_comparisons = 16384
  batch_size = 128
  learning_rate = 1e-3

  # Logging
  log_root = os.path.join("output", "train_preferences")  # output directory


@train_preferences_ex.named_config
def fast():
  """Small number of epochs, finish quickly, intended for tests / debugging."""
  total_comparisons = 256
# pylint:enable=unused-variable


train_preferences_ex.config(regress_utils.logging_config)


@train_preferences_ex.main
def train_preferences(_seed: int,  # pylint:disable=invalid-name
                      # Dataset
                      env_name: str,
                      num_vec: int,
                      policy_type: str,
                      policy_path: str,
                      # Target specification
                      target_reward_type: str,
                      target_reward_path: str,
                      # Model parameters
                      model_reward_type: regress_utils.EnvRewardFactory,
                      total_comparisons: int,
                      batch_size: int,
                      learning_rate: float,
                      # Logging
                      log_dir: str,
                     ) -> Mapping[str, Any]:
  """Entry-point into script for synthetic preference comparisons."""
  venv = util.make_vec_env(env_name, n_envs=num_vec, seed=_seed)

  def make_trainer(model, model_scope, target):
    del target
    model_params = model_scope.global_variables()
    kwargs = {"learning_rate": learning_rate}
    return preferences.PreferenceComparisonTrainer(model,
                                                   model_params,
                                                   batch_size=batch_size,
                                                   optimizer_kwargs=kwargs)

  with policies_serialize.load_policy(policy_type, policy_path, venv) as policy:
    def do_training(target, trainer):
      return trainer.fit_synthetic(venv,
                                   policy=policy,
                                   target=target,
                                   total_comparisons=total_comparisons)

    return regress_utils.regress(seed=_seed,
                                 venv=venv,
                                 make_trainer=make_trainer,
                                 do_training=do_training,
                                 target_reward_type=target_reward_type,
                                 target_reward_path=target_reward_path,
                                 model_reward_type=model_reward_type,
                                 log_dir=log_dir)


if __name__ == "__main__":
  main = script_utils.make_main(train_preferences_ex, "train_preferences")
  app.run(main)