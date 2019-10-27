# Copyright 2019 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#            http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Methods to create dataset generators.

These are methods that yield batches of observation-action-next observation
triples, implicitly used to define a distribution which distance metrics
can be taken with respect to.
"""

import contextlib
import math
from typing import Callable, ContextManager, Iterator, Union

import gym
from imitation.policies import serialize
from imitation.util import rollout, util
import numpy as np
from stable_baselines.common import base_class, policies, vec_env

from evaluating_rewards import rewards

BatchCallable = Callable[[int, int], Iterator[rewards.Batch]]
# Expect DatasetFactory to accept a str specifying env_name as first argument,
# int specifying seed as second argument and factory-specific keyword arguments
# after this. There is no way to specify this in Python type annotations yet :(
# See https://github.com/python/mypy/issues/5876
DatasetFactory = Callable[..., ContextManager[BatchCallable]]


@contextlib.contextmanager
def rollout_policy_generator(
    venv: vec_env.VecEnv, policy: Union[base_class.BaseRLModel, policies.BasePolicy]
) -> Iterator[BatchCallable]:
    """Generator returning rollouts from a policy in a given environment."""

    def f(total_timesteps: int, batch_size: int) -> Iterator[rewards.Batch]:
        nbatch = math.ceil(total_timesteps / batch_size)
        for _ in range(nbatch):
            transitions = rollout.generate_transitions(policy, venv, n_timesteps=batch_size)
            # TODO(): can we switch to rollout.Transition?
            yield rewards.Batch(
                obs=transitions.obs, actions=transitions.acts, next_obs=transitions.next_obs
            )

    yield f


@contextlib.contextmanager
def rollout_serialized_policy_generator(
    env_name: str, policy_type: str, policy_path: str, num_vec: int = 8, seed: int = 0
) -> Iterator[BatchCallable]:
    venv = util.make_vec_env(env_name, n_envs=num_vec, seed=seed)
    with serialize.load_policy(policy_type, policy_path, venv) as policy:
        with rollout_policy_generator(venv, policy) as generator:
            yield generator


@contextlib.contextmanager
def random_transition_generator(env_name: str, seed: int = 0) -> Iterator[BatchCallable]:
    """Randomly samples state and action and computes next state from dynamics.

    This is one of the weakest possible priors, with broad support. It is similar
    to `rollout_generator` with a random policy, with two key differences.
    First, adjacent timesteps are independent from each other, as a state
    is randomly sampled at the start of each transition. Second, the initial
    state distribution is ignored. WARNING: This can produce physically impossible
    states, if there is no path from a feasible initial state to a sampled state.

    Args:
        env_name: The name of a Gym environment. It must be a ResettableEnv.
        seed: Used to seed the dynamics.

    Yields:
        A function that, when called with timesteps and batch size, will perform
        the sampling process described above.
    """
    env = gym.make(env_name)
    env.seed(seed)
    # TODO(): why is total_timesteps specified here?
    # Could instead make it endless, or specify nbatch directly.

    def f(total_timesteps: int, batch_size: int) -> Iterator[rewards.Batch]:
        """Helper function."""
        nbatch = math.ceil(total_timesteps / batch_size)
        for _ in range(nbatch):
            obses = []
            acts = []
            next_obses = []
            for _ in range(batch_size):
                old_state = env.state_space.sample()
                obs = env.obs_from_state(old_state)
                act = env.action_space.sample()
                new_state = env.transition(old_state, act)  # may be non-deterministic
                next_obs = env.obs_from_state(new_state)

                obses.append(obs)
                acts.append(act)
                next_obses.append(next_obs)
            yield rewards.Batch(
                obs=np.array(obses), actions=np.array(acts), next_obs=np.array(next_obses)
            )

    yield f