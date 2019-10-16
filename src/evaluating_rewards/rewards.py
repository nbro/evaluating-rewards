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

"""Deep neural network reward models."""

import abc
import itertools
import os
import pickle
from typing import Dict, Iterable, Mapping, NamedTuple, Optional, Sequence, Tuple, Type, TypeVar

from absl import logging
import gym
from imitation.rewards import reward_net
from imitation.util import rollout, serialize
import numpy as np
from stable_baselines.common import input as env_in  # avoid name clash
import tensorflow as tf

K = TypeVar("K")


class AffineParameters(NamedTuple):
    """Parameters of an affine transformation.

    Attributes:
        constant: The additive shift.
        scale: The multiplicative dilation factor.
    """

    constant: float
    scale: float


class Batch(NamedTuple):
    """A batch of training data, consisting of obs-act-next obs transitions.

    Attributes:
        obs: Observations. Shape (batch_size, ) + obs_shape.
        actions: Actions. Shape (batch_size, ) + act_shape.
        next_obs: Observations. Shape (batch_size, ) + obs_shape.
    """

    # TODO(): switch to rollout.TransitionsNoRew, see imitation issue #95
    obs: np.ndarray
    actions: np.ndarray
    next_obs: np.ndarray


class RewardModel(serialize.Serializable):
    """Abstract reward model."""

    @property
    @abc.abstractmethod
    def reward(self) -> tf.Tensor:
        """Gets the reward output tensor."""

    @property
    @abc.abstractmethod
    def observation_space(self) -> gym.Space:
        """Gets the space of observations."""

    @property
    @abc.abstractmethod
    def action_space(self) -> gym.Space:
        """Gets the space of actions."""

    # TODO(): Avoid using multiple placeholders?
    # When dependencies move to TF 2.x API we can upgrade and this will be
    # unnecessary. Alternatively, we could create placeholders just once
    # and feed them in at construction, but this might require modification
    # to third-party codebases.
    @property
    @abc.abstractmethod
    def obs_ph(self) -> Iterable[tf.Tensor]:
        """Gets the current observation placeholder(s)."""

    @property
    @abc.abstractmethod
    def act_ph(self) -> Iterable[tf.Tensor]:
        """Gets the action placeholder(s)."""

    @property
    @abc.abstractmethod
    def next_obs_ph(self) -> Iterable[tf.Tensor]:
        """Gets the next observation placeholder(s)."""


class BasicRewardModel(RewardModel):
    """Abstract reward model class with basic default implementations."""

    def __init__(self, obs_space: gym.Space, act_space: gym.Space):
        RewardModel.__init__(self)
        self._obs_space = obs_space
        self._act_space = act_space
        self._obs_ph, self._proc_obs = env_in.observation_input(obs_space)
        self._next_obs_ph, self._proc_next_obs = env_in.observation_input(obs_space)
        self._act_ph, self._proc_act = env_in.observation_input(act_space)

    @property
    def observation_space(self):
        return self._obs_space

    @property
    def action_space(self):
        return self._act_space

    @property
    def obs_ph(self):
        return (self._obs_ph,)

    @property
    def next_obs_ph(self):
        return (self._next_obs_ph,)

    @property
    def act_ph(self):
        return (self._act_ph,)


class MLPRewardModel(BasicRewardModel, serialize.LayersSerializable):
    """Feed-forward reward model r(s,a,s')."""

    def __init__(
        self,
        obs_space: gym.Space,
        act_space: gym.Space,
        hid_sizes: Optional[Iterable[int]] = None,
        use_act: bool = True,
        use_obs: bool = True,
        use_next_obs: bool = True,
    ):
        BasicRewardModel.__init__(self, obs_space, act_space)
        if hid_sizes is None:
            hid_sizes = [32, 32]
        params = dict(locals())

        kwargs = {
            "obs_input": self._proc_obs if use_obs else None,
            "next_obs_input": self._proc_next_obs if use_next_obs else None,
            "act_input": self._proc_act if use_act else None,
        }
        self._reward, self.layers = reward_net.build_basic_theta_network(hid_sizes, **kwargs)
        serialize.LayersSerializable.__init__(**params, layers=self.layers)

    @property
    def reward(self):
        return self._reward


class PotentialShaping(BasicRewardModel, serialize.LayersSerializable):
    r"""Models a state-only potential, reward is the difference in potential.

    Specifically, contains a state-only potential $$\phi(s)$$. The reward
    $$r(s,a,s') = \gamma \phi(s') - \phi(s)$$ where $$\gamma$$ is the discount.
    """

    def __init__(
        self,
        obs_space: gym.Space,
        act_space: gym.Space,
        hid_sizes: Optional[Iterable[int]] = None,
        discount: float = 0.99,
        **kwargs,
    ):
        BasicRewardModel.__init__(self, obs_space, act_space)

        if hid_sizes is None:
            hid_sizes = [32, 32]
        params = dict(locals())
        del params["kwargs"]
        params.update(**kwargs)

        res = reward_net.build_basic_phi_network(
            hid_sizes, self._proc_obs, self._proc_next_obs, **kwargs
        )
        self._old_potential, self._new_potential, layers = res
        self.discount = discount
        self._reward_output = discount * self._new_potential - self.old_potential

        serialize.LayersSerializable.__init__(**params, layers=layers)

    @property
    def reward(self):
        return self._reward_output

    @property
    def old_potential(self):
        return self._old_potential

    @property
    def new_potential(self):
        return self._new_potential


class ConstantLayer(tf.keras.layers.Layer):
    """A layer that computes the same output, regardless of input.

    The output is a constant, repeated to the same shape as the input.
    The constant is a trainable variable, and can also be assigned to explicitly.
    """

    def __init__(
        self,
        name: str = None,
        initializer: Optional[tf.keras.initializers.Initializer] = None,
        dtype: tf.dtypes.DType = tf.float32,
    ):
        """Constructs a ConstantLayer.

        Args:
            name: String name of the layer.
            initializer: The initializer to use for the constant weight.
            dtype: dtype of the constant weight.
        """
        if initializer is None:
            initializer = tf.zeros_initializer()
        self.initializer = initializer

        super().__init__(trainable=True, name=name, dtype=dtype)

    def build(self, input_shape):
        self._constant = self.add_weight(
            name="constant", shape=(), initializer=self.initializer, use_resource=True
        )
        super().build(input_shape)

    def _check_built(self):
        if not self.built:
            raise ValueError("Must call build() before calling this function.")

    @property
    def constant(self):
        self._check_built()
        return self._constant

    def set_constant(self, val):
        self._check_built()
        self.set_weights([np.array(val)])

    def call(self, inputs):
        return inputs * 0 + self.constant

    def get_config(self):
        return {"name": self.name, "initializer": self.initializer, "dtype": self.dtype}


class ConstantReward(BasicRewardModel, serialize.LayersSerializable):
    """Outputs a constant reward value. Constant is a (trainable) variable."""

    def __init__(
        self,
        obs_space: gym.Space,
        act_space: gym.Space,
        initializer: Optional[tf.keras.initializers.Initializer] = None,
    ):
        BasicRewardModel.__init__(self, obs_space, act_space)
        params = dict(locals())

        self._constant = ConstantLayer(name="constant", initializer=initializer)
        n_batch = tf.shape(self._proc_obs)[0]
        obs = tf.reshape(self._proc_obs, [n_batch, -1])
        # self._reward_output is a scalar constant repeated (n_batch, ) times
        self._reward_output = self._constant(obs[:, 0])

        serialize.LayersSerializable.__init__(**params, layers={"constant": self._constant})

    @property
    def constant(self):
        return self._constant

    @property
    def reward(self):
        return self._reward_output


class ZeroReward(BasicRewardModel, serialize.LayersSerializable):
    """A reward model that always outputs zero."""

    def __init__(self, obs_space: gym.Space, act_space: gym.Space):
        serialize.LayersSerializable.__init__(**locals(), layers={})
        BasicRewardModel.__init__(self, obs_space, act_space)

        n_batch = tf.shape(self._proc_obs)[0]
        self._reward_output = tf.fill((n_batch,), 0.0)

    @property
    def reward(self):
        return self._reward_output


class RewardNetToRewardModel(RewardModel):
    """Adapts an (imitation repo) RewardNet to our RewardModel type."""

    def __init__(self, network: reward_net.RewardNet, use_test: bool = True):
        """Builds a RewardNet from a RewardModel.

        Args:
            network: A RewardNet.
            use_test: if True, uses `reward_output_test`; otherwise, uses
                    `reward_output_train`.
        """
        RewardModel.__init__(self)
        self.reward_net = network
        self.use_test = use_test

    @property
    def reward(self):
        if self.use_test:
            return self.reward_net.reward_output_test
        else:
            return self.reward_net.reward_output_train

    @property
    def observation_space(self):
        return self.reward_net.observation_space

    @property
    def action_space(self):
        return self.reward_net.action_space

    @property
    def obs_ph(self):
        return (self.reward_net.obs_ph,)

    @property
    def act_ph(self):
        return (self.reward_net.act_ph,)

    @property
    def next_obs_ph(self):
        return (self.reward_net.next_obs_ph,)

    @classmethod
    def _load(cls, directory: str) -> "RewardNetToRewardModel":
        with open(os.path.join(directory, "use_test"), "rb") as f:
            use_test = pickle.load(f)

        net = reward_net.RewardNet.load(os.path.join(directory, "net"))
        return cls(net, use_test=use_test)

    def _save(self, directory: str) -> None:
        with open(os.path.join(directory, "use_test"), "wb") as f:
            pickle.dump(self.use_test, f)

        self.reward_net.save(os.path.join(directory, "net"))


class RewardModelWrapper(RewardModel):
    """Wraper for RewardModel objects.

    This wraper is the identity; it is intended to be subclassed.
    """

    def __init__(self, model: RewardModel):
        """Builds a RewardNet from a RewardModel.

        Args:
            model: A RewardNet.
        """
        RewardModel.__init__(self)
        self.model = model

    @property
    def reward(self):
        return self.model.reward

    @property
    def observation_space(self):
        return self.model.observation_space

    @property
    def action_space(self):
        return self.model.action_space

    @property
    def obs_ph(self):
        return self.model.obs_ph

    @property
    def act_ph(self):
        return self.model.act_ph

    @property
    def next_obs_ph(self):
        return self.model.next_obs_ph

    @classmethod
    def _load(cls: Type[serialize.T], directory: str) -> serialize.T:
        model = RewardModel.load(os.path.join(directory, "model"))
        return cls(model)

    def _save(self, directory: str) -> None:
        self.model.save(os.path.join(directory, "model"))


class StopGradientsModelWrapper(RewardModelWrapper):
    """Stop gradients propagating through a reward model."""

    @property
    def reward(self):
        return tf.stop_gradient(super().reward)


class LinearCombinationModelWrapper(RewardModelWrapper):
    """Builds a linear combination of different reward models."""

    def __init__(self, models: Mapping[str, Tuple[RewardModel, tf.Tensor]]):
        """Constructs a reward model that linearly combines other reward models.

        Args:
            models: A mapping from ids to a tuple of a reward model and weight.
        """
        model = list(models.values())[0][0]
        for m, _ in models.values():
            assert model.action_space == m.action_space
            assert model.observation_space == m.observation_space
        super().__init__(model)
        self._models = models

        weighted = [weight * model.reward for model, weight in models.values()]
        self._reward_output = tf.reduce_sum(weighted, axis=0)

    @property
    def models(self) -> Mapping[str, Tuple[RewardModel, tf.Tensor]]:
        """Models we are linearly combining."""
        return self._models

    @property
    def reward(self):
        return self._reward_output

    @property
    def obs_ph(self):
        return tuple(itertools.chain(*[m.obs_ph for m, _ in self.models.values()]))

    @property
    def next_obs_ph(self):
        return tuple(itertools.chain(*[m.next_obs_ph for m, _ in self.models.values()]))

    @property
    def act_ph(self):
        return tuple(itertools.chain(*[m.act_ph for m, _ in self.models.values()]))

    @classmethod
    def _load(cls, directory: str) -> "LinearCombinationModelWrapper":
        """Restore dehydrated LinearCombinationModelWrapper.

        This should preserve the outputs of the original model, but the model
        itself may differ in two ways. The returned model is always an instance
        of LinearCombinationModelWrapper, and *not* any subclass it may have
        been created by (unless that subclass overrides save and load explicitly).
        Furthermore, the weights are frozen, and so will not be updated with
        training.

        Args:
            directory: The root of the directory to load the model from.

        Returns:
            An instance of LinearCombinationModelWrapper, making identical
            predictions as the saved model.
        """
        with open(os.path.join(directory, "linear_combination.pkl"), "rb") as f:
            loaded = pickle.load(f)

        models = {}
        for model_name, frozen_weight in loaded.items():
            model = RewardModel.load(os.path.join(directory, model_name))
            models[model_name] = (model, tf.constant(frozen_weight))

        return LinearCombinationModelWrapper(models)

    def _save(self, directory) -> None:
        """Save weights and the constituent models.

        WARNING: the weights will be evaluated and their values saved. This
        method makes no attempt to distinguish between constant weights (the common
        case) and variables or other tensors.

        Args:
            directory: The root of the directory to save the model to.
        """
        weights = {}
        for model_name, (model, weight) in self.models.items():
            model.save(os.path.join(directory, model_name))
            weights[model_name] = weight

        sess = tf.get_default_session()
        evaluated_weights = sess.run(weights)

        with open(os.path.join(directory, "linear_combination.pkl"), "wb") as f:
            pickle.dump(evaluated_weights, f)


class AffineTransform(LinearCombinationModelWrapper):
    """Positive affine transformation of a reward model.

    The scale and shift parameter are initialized to be the identity (scale one,
    shift zero).
    """

    def __init__(self, wrapped: RewardModel, scale: bool = True, shift: bool = True):
        """Wraps wrapped, adding a shift and scale parameter if specified.

        Args:
            wrapped: The RewardModel to wrap.
            scale: If true, adds a positive scale parameter.
            shift: If true, adds a shift parameter.
        """
        self._log_scale_layer = None
        if scale:
            self._log_scale_layer = ConstantLayer("log_scale")
            self._log_scale_layer.build(())
            scale = tf.exp(self._log_scale_layer.constant)  # force to be non-negative
        else:
            scale = tf.constant(1.0)

        models = {"wrapped": (wrapped, scale)}

        if shift:
            constant = ConstantReward(wrapped.observation_space, wrapped.action_space)
        else:
            constant = ZeroReward(wrapped.observation_space, wrapped.action_space)
        models["constant"] = (constant, tf.constant(1.0))

        super().__init__(models)

    def pretrain(
        self, batch: Batch, target: RewardModel, original: Optional[RewardModel] = None, eps=1e-8
    ) -> AffineParameters:
        """Initializes the shift and scale parameter to try to match target.

        Computes the mean and standard deviation of the wrapped reward model
        and target on batch, and sets the shift and scale parameters so that the
        output of this model has the same mean and standard deviation as target.

        If the wrapped model is just an affine transformation of target, this
        should get the correct values (given adequate data). However, if they differ
        -- even if just by potential shaping -- it can deviate substantially. It's
        generally still better than just the identity initialization.

        Args:
            batch: Data to evaluate the reward models on.
            target: A RewardModel to match the mean and standard deviation of.
            original: If specified, a RewardModel to rescale to match target.
                Defaults to using the reward model this object wraps, `self.wrapped`.
                This can be undesirable if `self.wrapped` includes some randomly
                initialized model elements, such as potential shaping, that would
                be better to treat as mean-zero.
            eps: Minimum standard deviation (for numerical stability).

        Returns:
            The initial shift and scale parameters.
        """
        if original is None:
            original = self.models["wrapped"][0]

        feed_dict = make_feed_dict([original, target], batch)
        sess = tf.get_default_session()
        preds = sess.run([original.reward, target.reward], feed_dict=feed_dict)
        original_mean, target_mean = np.mean(preds, axis=-1)
        original_std, target_std = np.clip(np.std(preds, axis=-1), eps, None)

        log_scale = 0.0
        if self._log_scale_layer is not None:
            log_scale = np.log(target_std) - np.log(original_std)
            logging.info("Assigning log scale: %f", log_scale)
            self._log_scale_layer.set_constant(log_scale)
        scale = np.exp(log_scale)

        constant = 0.0
        constant_model = self.models["constant"][0]
        if isinstance(constant_model, ConstantReward):
            constant = -original_mean * target_std / original_std + target_mean
            logging.info("Assigning shift: %f", constant)
            constant_model.constant.set_constant(constant)

        return AffineParameters(constant=constant, scale=scale)

    @property
    def constant(self) -> tf.Tensor:
        """The additive shift."""
        return self.models["constant"][0].constant.constant

    @property
    def scale(self) -> tf.Tensor:
        """The multiplicative dilation."""
        return self.models["wrapped"][1]

    def get_weights(self):
        """Extract affine parameters from a model.

        Returns:
            The current affine parameters (scale and shift), from the perspective of
            mapping the *original* onto the *target*; that is, the inverse of the
            transformation actually performed in the model. (This is for ease of
            comparison with results returned by other methods.)
        """
        sess = tf.get_default_session()
        const, scale = sess.run([self.constant, self.scale])
        return AffineParameters(constant=const, scale=scale)

    @classmethod
    def _load(cls, directory: str) -> "AffineTransform":
        """Load an AffineTransform.

        We use the same saving logic as LinearCombinationModelWrapper. This works
        as AffineTransform does not have any extra state needed for inference.
        (There is self._log_scale which is used in pretraining.)

        Args:
            directory: The directory to load from.

        Returns:
            The deserialized AffineTransform instance.
        """
        obj = cls.__new__(cls)
        lc = LinearCombinationModelWrapper._load(directory)
        LinearCombinationModelWrapper.__init__(obj, lc.models)
        return obj


class PotentialShapingWrapper(LinearCombinationModelWrapper):
    """Adds potential shaping to an underlying reward model."""

    def __init__(self, wrapped: RewardModel, **kwargs):
        """Wraps wrapped with a PotentialShaping instance.

        Args:
            wrapped: The model to add shaping to.
            **kwargs: Passed through to PotentialShaping.
        """
        shaping = PotentialShaping(wrapped.observation_space, wrapped.action_space, **kwargs)

        super().__init__(
            {"wrapped": (wrapped, tf.constant(1.0)), "shaping": (shaping, tf.constant(1.0))}
        )


def make_feed_dict(models: Iterable[RewardModel], batch: Batch) -> Dict[tf.Tensor, np.ndarray]:
    """Construct a feed dictionary for models for data in batch."""
    assert batch.obs.shape == batch.next_obs.shape
    assert batch.obs.shape[0] == batch.actions.shape[0]
    a_model = next(iter(models))
    assert batch.obs.shape[1:] == a_model.observation_space.shape
    assert batch.actions.shape[1:] == a_model.action_space.shape
    for m in models:
        assert a_model.observation_space == m.observation_space
        assert a_model.action_space == m.action_space

    feed_dict = {}
    for m in models:
        feed_dict.update({ph: batch.obs for ph in m.obs_ph})
        feed_dict.update({ph: batch.actions for ph in m.act_ph})
        feed_dict.update({ph: batch.next_obs for ph in m.next_obs_ph})

    return feed_dict


def evaluate_models(models: Mapping[K, RewardModel], batch: Batch) -> Mapping[K, np.ndarray]:
    """Computes prediction of reward models."""
    reward_outputs = {k: m.reward for k, m in models.items()}
    feed_dict = make_feed_dict(models.values(), batch)
    return tf.get_default_session().run(reward_outputs, feed_dict=feed_dict)


def compute_returns(
    models: Mapping[K, RewardModel], trajectories: Sequence[rollout.Trajectory]
) -> Mapping[K, np.ndarray]:
    """Computes the (undiscounted) returns of each trajectory under each model.

    Args:
        models: A collection of reward models.
        trajectories: A sequence of trajectories.

    Returns:
        A collection of NumPy arrays containing the returns from each model.
    """
    # Reward models are Markovian so only operate on a timestep at a time,
    # expecting input shape (batch_size, ) + {obs,act}_shape. Flatten the
    # trajectories to accommodate this.
    transitions = rollout.flatten_trajectories(trajectories)
    flattened = Batch(obs=transitions.obs, actions=transitions.acts, next_obs=transitions.next_obs)
    preds = evaluate_models(models, flattened)

    # To compute returns, we must sum over slices of the flattened reward
    # sequence corresponding to each episode. Find the episode boundaries.
    ep_boundaries = np.where(transitions.dones)[0]
    # NumPy equivalent of Python ep_boundaries = [0] + ep_boundaries[:-1]
    idxs = np.pad(ep_boundaries[:-1], (1, 0), "constant")
    # ep_boundaries is inclusive, but reduceat takes exclusive range
    idxs = idxs + 1
    # Now, sum over the slices.
    ep_returns = {k: np.add.reduceat(v, idxs) for k, v in preds.items()}

    return ep_returns


def evaluate_potentials(potentials: Iterable[PotentialShaping], batch: Batch) -> np.ndarray:
    """Computes prediction of potential shaping models."""
    old_pots = [p.old_potential for p in potentials]
    new_pots = [p.new_potential for p in potentials]
    feed_dict = make_feed_dict(potentials, batch)
    return tf.get_default_session().run([old_pots, new_pots], feed_dict=feed_dict)