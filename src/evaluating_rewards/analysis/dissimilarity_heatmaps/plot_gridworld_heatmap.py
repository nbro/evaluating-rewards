# Copyright 2020 Adam Gleave
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

"""CLI script to plot heatmap of dissimilarity between reward functions in gridworlds."""

import collections
import os
from typing import Any, Dict, Iterable, Mapping, Optional

from imitation.util import util
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sacred

from evaluating_rewards import serialize, tabular
from evaluating_rewards.analysis import gridworld_rewards, stylesheets, visualize
from evaluating_rewards.analysis.dissimilarity_heatmaps import heatmaps, reward_masks
from evaluating_rewards.analysis.reward_figures import gridworld_reward_heatmap
from evaluating_rewards.scripts import script_utils

plot_gridworld_heatmap_ex = sacred.Experiment("plot_gridworld_heatmap")


@plot_gridworld_heatmap_ex.config
def default_config():
    """Default configuration values."""
    normalize = False

    # Dataset parameters
    log_root = serialize.get_output_dir()  # where results are read from/written to
    discount = 0.99
    reward_subset = None

    # Figure parameters
    kind = "npec"
    styles = ["paper", "heatmap", "heatmap-1col", "heatmap-1col-fatlabels", "tex"]
    save_kwargs = {
        "fmt": "pdf",
    }

    _ = locals()
    del _


@plot_gridworld_heatmap_ex.config
def heatmap_kwargs_default(kind):
    heatmap_kwargs = {  # noqa: F841  pylint:disable=unused-variable
        "masks": {kind: [reward_masks.always_true]},
        "fmt": lambda x: f"{x:.4f}",
        "log": kind == "direct_divergence",
    }


@plot_gridworld_heatmap_ex.named_config
def test():
    """Unit tests/debugging."""
    styles = ["paper", "heatmap", "heatmap-2col"]  # disable TeX
    reward_subset = ["sparse_goal", "dense_goal"]
    _ = locals()
    del _


@plot_gridworld_heatmap_ex.named_config
def normalize_distance(reward_subset):
    normalize = True  # noqa: F841  pylint:disable=unused-variable
    reward_subset += ["evaluating_rewards/Zero-v0"]


@plot_gridworld_heatmap_ex.named_config
def paper():
    """Figure for paper appendix."""
    reward_subset = [
        "sparse_goal",
        "transformed_goal",
        "center_goal",
        "sparse_penalty",
        "dirt_path",
        "cliff_walk",
    ]
    heatmap_kwargs = {  # noqa: F841  pylint:disable=unused-variable
        "order": reward_subset,
        "cbar_kws": dict(fraction=0.05),
    }


@plot_gridworld_heatmap_ex.config
def logging_config(log_root):
    log_dir = os.path.join(  # noqa: F841  pylint:disable=unused-variable
        log_root,
        "plot_gridworld_divergence",
        util.make_unique_timestamp(),
    )


def state_to_3d(reward: np.ndarray, ns: int, na: int) -> np.ndarray:
    """Convert state-only reward R[s] to 3D reward R[s,a,s'].

    Args:
        - reward: state only reward.
        - ns: number of states.
        - na: number of actions.

    Returns:
        State-action-next state reward from tiling `reward`.
    """
    assert reward.ndim == 1
    assert reward.shape[0] == ns
    return np.tile(reward[:, np.newaxis, np.newaxis], (1, na, ns))


def grid_to_3d(reward: np.ndarray) -> np.ndarray:
    """Convert gridworld state-only reward R[i,j] to 3D reward R[s,a,s']."""
    assert reward.ndim == 2
    reward = reward.flatten()
    ns = reward.shape[0]
    return state_to_3d(reward, ns, 5)


def make_reward(cfg: Dict[str, np.ndarray], discount: float) -> np.ndarray:
    """Create reward from state-only reward and potential."""
    state_reward = grid_to_3d(cfg["state_reward"])
    potential = cfg["potential"]
    assert potential.ndim == 2  # gridworld, (i,j) indexed
    potential = potential.flatten()
    return tabular.shape(state_reward, potential, discount)


def build_dist(rew: np.ndarray, xlen: int, ylen: int) -> np.ndarray:
    """Computes uniform visitation distribution compatible with gridworld dynamics.

    Args:
        rew: A three-dimensional reward (needed for dimensionality).
        xlen: width of gridworld.
        ylen: height of gridworld.

    Returns:
        A distribution
    """
    ns, na, ns2 = rew.shape
    assert ns == xlen * ylen
    assert ns == ns2
    transitions = gridworld_reward_heatmap.build_transitions(xlen, ylen, na).transpose((1, 0, 2))
    return transitions / np.sum(transitions)


CANONICAL_DESHAPE_FN = {
    "singleton_canonical": tabular.singleton_shaping_canonical_reward,
    "fully_connected_random_canonical": tabular.fully_connected_random_canonical_reward,
    "fully_connected_greedy_canonical": tabular.fully_connected_greedy_canonical_reward,
}


def compute_divergence(reward_cfg: Dict[str, Any], discount: float, kind: str) -> pd.Series:
    """Compute divergence for each pair of rewards in `reward_cfg`."""
    rewards = {name: make_reward(cfg, discount) for name, cfg in reward_cfg.items()}
    divergence = collections.defaultdict(dict)
    for src_name, src_reward in rewards.items():
        for target_name, target_reward in rewards.items():
            if target_name == "evaluating_rewards/Zero-v0":
                continue
            xlen, ylen = reward_cfg[src_name]["state_reward"].shape
            distribution = build_dist(src_reward, xlen, ylen)

            if kind == "npec":
                div = tabular.npec_distance(
                    src_reward, target_reward, dist=distribution, n_iter=1000, discount=discount
                )
            elif kind == "asymmetric":
                div = tabular.asymmetric_distance(
                    src_reward, target_reward, dist=distribution, n_iter=1000, discount=discount
                )
            elif kind in ["symmetric", "symmetric_min"]:
                use_min = kind == "symmetric_min"
                div = tabular.symmetric_distance(
                    src_reward,
                    target_reward,
                    dist=distribution,
                    n_iter=1000,
                    discount=discount,
                    use_min=use_min,
                )
            elif kind.endswith("_direct") or kind.endswith("_pearson"):
                if kind.endswith("_direct"):
                    distance_fn = tabular.canonical_reward_distance
                else:
                    distance_fn = tabular.deshape_pearson_distance

                canonical_kind = "_".join(kind.split("_")[:-1])
                try:
                    deshape_fn = CANONICAL_DESHAPE_FN[canonical_kind]
                except KeyError as e:
                    raise ValueError(f"Invalid canonicalizer '{canonical_kind}'") from e

                div = distance_fn(
                    src_reward,
                    target_reward,
                    deshape_fn=deshape_fn,
                    dist=distribution,
                    discount=discount,
                )
            else:
                raise ValueError(f"Unrecognized kind '{kind}'")

            divergence[target_name][src_name] = div
    divergence = pd.DataFrame(divergence)
    divergence = divergence.stack()
    divergence.index.names = ["source_reward_type", "target_reward_type"]
    return divergence


@plot_gridworld_heatmap_ex.main
def plot_gridworld_heatmap(
    normalize: bool,
    styles: Iterable[str],
    reward_subset: Optional[Iterable[str]],
    heatmap_kwargs: Dict[str, Any],
    kind: str,
    discount: float,
    log_dir: str,
    save_kwargs: Mapping[str, Any],
) -> None:
    """Entry-point into script to produce divergence heatmaps.

    Args:
        normalize: whether to divide by distance from Zero.
        styles: styles to apply from `evaluating_rewards.analysis.stylesheets`.
        reward_subset: if specified, subset of keys to plot.
        discount: discount rate of MDP.
        log_dir: directory to write figures and other logging to.
        save_kwargs: passed through to `analysis.save_figs`.
    """
    with stylesheets.setup_styles(styles):
        rewards = gridworld_rewards.REWARDS
        if reward_subset is not None:
            rewards = {k: rewards[k] for k in reward_subset}
            divergence = compute_divergence(rewards, discount, kind)

        if normalize:
            divergence = heatmaps.normalize_dissimilarity(divergence)

        figs = heatmaps.compact_heatmaps(dissimilarity=divergence, **heatmap_kwargs)
        try:
            # Since tick labels are names not emojis for gridworlds, rotate to save space
            plt.xticks(rotation=45)
            plt.yticks(rotation=45)
            visualize.save_figs(log_dir, figs.items(), **save_kwargs)
        finally:
            for fig in figs:
                plt.close(fig)


if __name__ == "__main__":
    script_utils.experiment_main(plot_gridworld_heatmap_ex, "plot_gridworld_heatmap")
