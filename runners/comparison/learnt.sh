#!/usr/bin/env bash
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

# Compare hardcoded rewards in PointMass to each other

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
. ${DIR}/../common.sh

TRAIN_CMD=$(call_script "model_comparison" "with")

learnt_model $*  # sets learnt_model_dir, source_reward_type and model_name

echo "Starting model comparison"
for env_name in ${ENVS}; do
  echo "Model comparison for ${env_name}"
  env_name_sanitized=$(echo ${env_name} | sed -e 's/\//_/g')
  MODELS=$(find ${learnt_model_dir}/${env_name_sanitized} -path "*/${model_name}" -printf "%P\n" | sed -e "s@/${model_name}\$@@")

  types=${REWARDS_BY_ENV[$env_name]}
  types_sanitized=$(echo ${types} | sed -e 's/\//_/g')

  echo "Comparing models to hardcoded rewards"
  echo "Models: ${MODELS}"
  echo "Hardcoded rewards: ${types}"

  parallel --header : --results ${EVAL_OUTPUT_ROOT}/parallel/comparison/learnt/${env_name_sanitized} \
    ${TRAIN_CMD} env_name=${env_name} seed={seed}  \
    source_reward_type=${source_reward_type} \
    source_reward_path=${learnt_model_dir}/${env_name_sanitized}/{source_reward}/${model_name} \
    target_reward_type={target_reward} {named_config} \
    log_dir=${EVAL_OUTPUT_ROOT}/comparison/${model_prefix}/${env_name_sanitized}/{source_reward}/match_{named_config}_to_{target_reward_sanitized}_seed{seed} \
    ::: source_reward ${MODELS} \
    ::: target_reward ${types} \
    :::+ target_reward_sanitized ${types_sanitized} \
    ::: named_config "" affine_only \
    ::: seed 0 1 2
done
