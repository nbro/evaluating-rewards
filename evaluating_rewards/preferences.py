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

"""Learning reward model from preference comparisons.

This is based on Deep Reinforcement Learning from Human Preferences
(Christiano et al, 2017), but is simplified in several ways. 1) It is only
intended to work with synthetic preference comparisons, not human data
(relatively sample inefficient with no replay buffer). 2) It uses just a single
reward model, not an ensemble, with no prioritized sampling. 3) No adaptive
regularization based on validation accuracy.
"""

import math
from typing import Any, Dict, Iterable, List, NamedTuple, Type

from absl import logging
from evaluating_rewards import rewards
from imitation.util import rollout
import numpy as np
import pandas as pd
from stable_baselines.common import policies
from stable_baselines.common import vec_env
import tensorflow as tf


class TrajectoryPreference(NamedTuple):
  """Two trajectories with a label indicating which is better.

  Attributes:
    - traja: the first trajectory, of length N.
    - trajb: the second trajectory, of length N.
    - label: 0 if traja is best, 1 if trajb is best.
  """

  traja: rollout.Trajectory
  trajb: rollout.Trajectory
  label: int


def _extend_placeholders(ph, name):
  return tf.placeholder(shape=(None, None) + ph.shape,
                        dtype=ph.dtype, name=name)


def _concatenate(preferences: List[TrajectoryPreference],
                 attr: str, idx: slice) -> np.ndarray:
  """Flattens pairs of trajectories.

  Args:
    preferences: a list of N trajectory comparisons.
    attr: the attribute in the trajectory of interest (e.g. 'obs', 'act').
    idx: a slice object describing the subset of the trajectory to extract.
        This must result in fixed M-length subsets.

  Returns:
    An array of shape (2 * N * M, ) + attr_shape.
  """
  traja = np.stack([getattr(p.traja, attr)[idx] for p in preferences])
  trajb = np.stack([getattr(p.trajb, attr)[idx] for p in preferences])
  stacked = np.stack([traja, trajb])
  return stacked.reshape((-1,) + stacked.shape[3:])


class PreferenceComparisonTrainer(object):
  """Fits a reward model based on binary comparisons between trajectories."""

  def __init__(self,
               model: rewards.RewardModel,
               # TODO(): implement get_parameters method in RewardModel
               # (It's awkward for caller to have to compute parameters.)
               model_params: Iterable[tf.Tensor],
               batch_size: int,
               optimizer: Type[tf.train.Optimizer] = tf.train.AdamOptimizer,
               optimizer_kwargs: Dict[str, Any] = None,
               regularization_weight: float = 0.0,
               reward_l2_reg: float = 1e-2,
               accuracy_threshold: float = 0.7):
    """Constructs a PreferenceComparisonTrainer for a reward model.

    Args:
      model: The reward model.
      model_params: The parameters belonging to the model to regularize.
      batch_size: The number of trajectories in each training epoch.
      optimizer: A TensorFlow optimizer.
      optimizer_kwargs: Parameters for the optimizer, e.g. learning rate.
      regularization_weight: The weight of regularizations on the parameters.
      reward_l2_reg: The weight of regularization on the outputs. This can be
          interpreted as a sparsity prior.
      accuracy_threshold: The minimum probability for a model prediction to be
          classified as preferring one trajectory to the other; below this,
          it will be a tie. (Set to 0.5 to eliminate ties.)
    """
    self._model = model
    self._model_params = model_params
    self._batch_size = batch_size
    self._reward_l2_reg = reward_l2_reg
    self._regularization_weight = regularization_weight
    self._accuracy_threshold = accuracy_threshold

    self._preference_labels = tf.placeholder(shape=(None,),
                                             dtype=tf.int32,
                                             name="preferred")

    train_losses = self._get_loss_ops()
    self._train_pure_loss = train_losses["pure_loss"]
    self._train_loss = train_losses["train_loss"]
    self._train_acc = train_losses["accuracy"]

    optimizer_kwargs = optimizer_kwargs or {}
    self._optimizer = optimizer(**optimizer_kwargs)
    self._train_op = self._optimizer.minimize(self._train_loss)

  def _get_regularizer(self):
    num_params = 0
    for t in self._model_params:
      assert t.shape.is_fully_defined()
      num_params += np.prod(t.shape.as_list())
    return sum(tf.nn.l2_loss(t) for t in self._model_params) / num_params

  def _get_returns(self):
    """Computes the undiscounted returns of each trajectory.

    Returns:
      A Tensor of shape (2, batch_size) consisting of the sum of the rewards
      of each trajectory.
    """
    # Predicted rewards for two trajectories.
    # self.model.reward shape: (2 * batch_size * trajectory_length)
    # pred_rewards shape: (2, batch_size, trajectory_length)
    pred_rewards = tf.reshape(self.model.reward, [2, self._batch_size, -1])
    # Reduce predicted rewards to undiscounted returns
    returns = tf.reduce_sum(pred_rewards, axis=2)

    return returns

  def _get_labeling_loss(self,
                         returns: tf.Tensor,
                         preference_labels: tf.Tensor) -> tf.Tensor:
    """Builds the cross-entropy labeling loss.

    Args:
      returns: A tensor containing undiscounted returns of each trajectory,
          of shape (2, batch_size).
      preference_labels: A tensor containing preference labels, of shape
          (2, batch_size), where preference_labels[i][j] is 1 if trajectory i
          is preferred in comparison j and 0 otherwise.

    Returns:
      The cross-entropy loss.
    """
    # Convert returns into log-probabilities
    log_probs = tf.nn.log_softmax(returns, axis=0)  # shape: (2, batch_size)
    # preferred_log_probs: log probability of trajectory specified in label
    # shape: (batch_size,)
    masked_log_probs = log_probs * tf.cast(preference_labels, tf.float32)
    preferred_log_probs = tf.reduce_sum(masked_log_probs, axis=0)
    # Average over batch
    labeling_loss = tf.reduce_mean(-preferred_log_probs)  # shape: ()

    return labeling_loss

  def _get_accuracy(self,
                    returns: tf.Tensor,
                    preference_labels: tf.Tensor) -> tf.Tensor:
    """Builds the accuracy of the model predictions.

    A prediction is considered accurate if the probability, computed from
    returns, is above self._accuracy_threshold in the same direction as
    preference_labels.

    Args:
      returns: A tensor containing undiscounted returns of each trajectory,
          of shape (2, batch_size).
      preference_labels: A tensor containing preference labels, of shape
          (2, batch_size), where preference_labels[i][j] is 1 if trajectory i
          is preferred in comparison j and 0 otherwise.

    Returns:
      The percentage of accurate predictions.
    """
    preference_probs = tf.nn.softmax(returns, axis=0)  # shape: (2, batch_size)

    def accuracy_helper(x):
      def false_fn():
        return tf.cast(tf.less(x[0], self._accuracy_threshold), tf.int32)
      return tf.cond(
          tf.greater(x[1], self._accuracy_threshold),
          true_fn=lambda: tf.constant(2),
          false_fn=false_fn)

    # Threshold predictions into one of three classes: 0 if 1st trajectory
    # better, 1 if tied (< accuracy threshold), 2 if 2nd trajectory better.
    predictions = tf.map_fn(accuracy_helper, tf.transpose(preference_probs),
                            dtype=tf.int32)  # shape: (batch_size,)
    # Map [0,1] labels to [0,2] for consistency with `accuracy_helper`.
    labels = preference_labels[1, :] * 2  # shape: (batch_size,)

    # Accuracy of overall predictions.
    # Note 1 ('tied') labels always count as inaccurate.
    accuracy = tf.contrib.metrics.accuracy(predictions=predictions,
                                           labels=labels)

    return accuracy

  def _get_loss_ops(self):
    """Returns loss to be optimized given a batch of experience."""
    returns = self._get_returns()

    # self._preference_labels are 0 (first trajectory > second trajectory)
    # or 1 (second > first), shape (batch_size, ).
    # preference_labels are shape (2, batch_size) and preference_label[i][j]
    # is 1 if trajectory i is preferred in comparison j.
    preference_labels = tf.stack([self._preference_labels,
                                  1 - self._preference_labels])

    labeling_loss = self._get_labeling_loss(returns, preference_labels)
    accuracy = self._get_accuracy(returns, preference_labels)

    # Reward prior for l2 regularization of output rewards.
    reward_prior = tf.reduce_mean(tf.square(self.model.reward))

    # Calculate regularizer.
    regularizer = self._get_regularizer()

    return {
        "pure_loss": labeling_loss,
        "regularizer": regularizer,
        "reward_prior": reward_prior,
        "accuracy": accuracy,
        "train_loss":
            labeling_loss + self._reward_l2_reg * reward_prior +
            self.regularization_weight * regularizer
    }

  def _make_feed_dict(self, preferences: List[TrajectoryPreference]):
    """Builds a feed dictionary.

    Args:
      preferences: A list of trajectory comparisons.

    Returns:
      A feed dict.
    """
    batch = rewards.Batch(
        obs=_concatenate(preferences, "obs", slice(0, -1)),
        actions=_concatenate(preferences, "act", slice(None)),
        next_obs=_concatenate(preferences, "obs", slice(1, None)),
    )
    feed_dict = rewards.make_feed_dict([self.model], batch)
    labels = np.array([p.label for p in preferences])
    feed_dict[self._preference_labels] = labels
    return feed_dict

  def train_one_batch(self, preferences: List[TrajectoryPreference]):
    """Performs one training epoch over the provided batch of preferences."""
    sess = tf.get_default_session()
    assert len(preferences) == self._batch_size
    feed_dict = self._make_feed_dict(preferences)
    ops = {
        "pure_loss": self._train_pure_loss,
        "training_loss": self._train_loss,
        "accuracy": self._train_acc,
        "opt_step": self._train_op,
    }
    output = sess.run(ops, feed_dict=feed_dict)
    del output["opt_step"]  # always None
    return output

  def fit_synthetic(self,
                    venv: vec_env.VecEnv,
                    policy: policies.BasePolicy,
                    target: rewards.RewardModel,
                    total_comparisons: int) -> pd.DataFrame:
    """Trains using synthetic comparisons from target.

    Args:
      venv: The environment to generate trajectories in.
          Must be fixed-horizon.
      policy: The policy to generate trajectories with.
      target: The reward model to compare the trajectories via.
      total_comparisons: The total number of trajectory *pairs* to compare.

    Returns:
      A dataframe containing training statistics.
    """
    n_batches = math.ceil(total_comparisons / self.batch_size)
    n_episodes = 2 * self.batch_size

    stats = {}
    for epoch in range(n_batches):
      trajectories = rollout.generate_trajectories(policy, venv,
                                                   n_episodes=n_episodes)

      traj_len = len(trajectories[0].act)
      for traj in trajectories:
        if len(traj.act) != traj_len:
          # TODO(): slice the trajectories so they're fixed length?
          # (Currently we require environment be fixed horizon.)
          # It also seems possible in principle to train on variable-length
          # trajectories, but a bit awkward (would probably need to pad?)
          raise ValueError("Trajectories must be fixed length.")

      returns = rewards.compute_returns({"t": target}, trajectories)["t"]

      batch = []
      for i in range(self.batch_size):
        preference = TrajectoryPreference(
            traja=trajectories[2*i],
            trajb=trajectories[2*i+1],
            label=int(returns[2*i] >= returns[2*i+1]),
        )
        batch.append(preference)

      res = self.train_one_batch(batch)
      for k, v in res.items():
        stats.setdefault(k, []).append(v)
      # TODO(): better logging, e.g. TensorBoard summaries
      logging.info(f"Epoch {epoch}: {res}")

    return pd.DataFrame(stats)

  @property
  def model(self):
    return self._model

  @property
  def batch_size(self):
    return self._batch_size

  @property
  def regularization_weight(self):
    return self._regularization_weight