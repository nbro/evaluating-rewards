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
import functools
from typing import Callable, ContextManager, Iterator, TypeVar, Union

import gym
from imitation.data import rollout, types
from imitation.policies import serialize
from imitation.util import util
import numpy as np
from stable_baselines.common import base_class, policies, vec_env

T = TypeVar("T")
DatasetCallable = Callable[[int], T]
"""Parameter specifies number of episodes (TrajectoryCallable) or timesteps (otherwise)."""
TrajectoryCallable = DatasetCallable[types.Trajectory]
TransitionsCallable = DatasetCallable[types.Transitions]
SampleDist = DatasetCallable[np.ndarray]

C = TypeVar("C")
Factory = Callable[..., ContextManager[C]]
TrajectoryFactory = Factory[TrajectoryCallable]
TransitionsFactory = Factory[TransitionsCallable]
SampleDistFactory = Factory[SampleDist]

# *** Conversion functions ***


@contextlib.contextmanager
def transitions_factory_iid_from_sample_dist(
    obs_dist: SampleDist, act_dist: SampleDist
) -> Iterator[TransitionsCallable]:
    """Samples state and next state i.i.d. from `obs_dist` and actions i.i.d. from `act_dist`.

    This is an extremely weak prior. It's most useful in conjunction with methods in
    `canonical_sample` which assume i.i.d. transitions internally.
    """

    def f(total_timesteps: int) -> types.Transitions:
        obses = obs_dist(total_timesteps)
        acts = act_dist(total_timesteps)
        next_obses = obs_dist(total_timesteps)
        dones = np.zeros(total_timesteps, dtype=np.bool)
        return types.Transitions(
            obs=np.array(obses),
            acts=np.array(acts),
            next_obs=np.array(next_obses),
            dones=dones,
            infos=None,
        )

    yield f


def transitions_callable_to_sample_dist(
    transitions_callable: TransitionsCallable, obs: bool
) -> SampleDist:
    """Samples state/actions from batches returned by `batch_callable`.

    If `obs` is true, then samples observations from state and next state.
    If `obs` is false, then samples actions.
    """

    def f(n: int) -> np.ndarray:
        num_timesteps = ((n - 1) // 2 + 1) if obs else n
        transitions = transitions_callable(num_timesteps)
        if obs:
            res = np.concatenate((transitions.obs, transitions.next_obs))
        else:
            res = transitions.acts
        return res[:n]

    return f


@contextlib.contextmanager
def transitions_factory_to_sample_dist_factory(
    transitions_factory: TransitionsFactory, obs: bool, **kwargs
) -> Iterator[SampleDist]:
    """Converts TransitionsFactory to a SampleDistFactory.

    See `transitions_callable_to_sample_dist`.
    """
    with transitions_factory(**kwargs) as transitions_callable:
        yield transitions_callable_to_sample_dist(transitions_callable, obs)


# *** Trajectory factories ***


@contextlib.contextmanager
def _factory_via_serialized(
    factory_from_policy: Callable[[vec_env.VecEnv, policies.BasePolicy], T],
    env_name: str,
    policy_type: str,
    policy_path: str,
    **kwargs,
) -> Iterator[T]:
    venv = util.make_vec_env(env_name, **kwargs)
    with serialize.load_policy(policy_type, policy_path, venv) as policy:
        with factory_from_policy(venv, policy) as generator:
            yield generator


@contextlib.contextmanager
def trajectory_factory_from_policy(
    venv: vec_env.VecEnv, policy: Union[base_class.BaseRLModel, policies.BasePolicy]
) -> Iterator[TransitionsCallable]:
    """Generator returning rollouts from a policy in a given environment."""

    def f(total_episodes: int) -> types.Transitions:
        return rollout.generate_trajectories(
            policy, venv, sample_until=rollout.min_episodes(total_episodes)
        )

    yield f


trajectory_factory_from_serialized_policy = functools.partial(
    _factory_via_serialized, trajectory_factory_from_policy
)


# *** Transition factories ***


@contextlib.contextmanager
def transitions_factory_from_policy(
    venv: vec_env.VecEnv, policy: Union[base_class.BaseRLModel, policies.BasePolicy]
) -> Iterator[TransitionsCallable]:
    """Generator returning rollouts from a policy in a given environment."""

    def f(total_timesteps: int) -> types.Transitions:
        # TODO(adam): inefficient -- discards partial trajectories and resets environment
        return rollout.generate_transitions(policy, venv, n_timesteps=total_timesteps)

    yield f


transitions_factory_from_serialized_policy = functools.partial(
    _factory_via_serialized, transitions_factory_from_policy
)


@contextlib.contextmanager
def transitions_factory_from_random_model(
    env_name: str, seed: int = 0
) -> Iterator[TransitionsCallable]:
    """Randomly samples state and action and computes next state from dynamics.

    This is one of the weakest possible priors, with broad support. It is similar
    to `transitions_factory_from_policy` with a random policy, with two key differences.
    First, adjacent timesteps are independent from each other, as a state
    is randomly sampled at the start of each transition. Second, the initial
    state distribution is ignored. WARNING: This can produce physically impossible
    states, if there is no path from a feasible initial state to a sampled state.

    Args:
        env_name: The name of a Gym environment. It must be a ResettableEnv.
        seed: Used to seed the dynamics.

    Yields:
        A function that will perform the sampling process described above for a
        number of timesteps specified in the argument.
    """
    env = gym.make(env_name)
    env.seed(seed)

    def f(total_timesteps: int) -> types.Transitions:
        """Helper function."""
        obses = []
        acts = []
        next_obses = []
        for _ in range(total_timesteps):
            old_state = env.state_space.sample()
            obs = env.obs_from_state(old_state)
            act = env.action_space.sample()
            new_state = env.transition(old_state, act)  # may be non-deterministic
            next_obs = env.obs_from_state(new_state)

            obses.append(obs)
            acts.append(act)
            next_obses.append(next_obs)
        dones = np.zeros(total_timesteps, dtype=np.bool)
        return types.Transitions(
            obs=np.array(obses),
            acts=np.array(acts),
            next_obs=np.array(next_obses),
            dones=dones,
            infos=None,
        )

    yield f


# *** Sample distribution factories ***


@contextlib.contextmanager
def sample_dist_from_space(space: gym.Space) -> Iterator[SampleDist]:
    """Creates function to sample `n` elements from from `space`."""

    def f(n: int) -> np.ndarray:
        return np.array([space.sample() for _ in range(n)])

    yield f


@contextlib.contextmanager
def sample_dist_from_env_name(env_name: str, obs: bool) -> Iterator[SampleDist]:
    env = gym.make(env_name)
    try:
        space = env.observation_space if obs else env.action_space
        with sample_dist_from_space(space) as sample_dist:
            yield sample_dist
    finally:
        env.close()
