#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
venv_dir="${VLLM_ENV_DIR:-${repo_root}/.venv-vllm}"

exec "${venv_dir}/bin/vllm" serve Qwen/Qwen2.5-Coder-32B-Instruct \
    --served-model-name 'qwen2.5-coder:32b' \
    --host "${VLLM_HOST:-127.0.0.1}" \
    --port "${VLLM_PORT:-8002}" \
    --api-key "${VLLM_API_KEY:-jitbench-local}" \
    --tensor-parallel-size "${VLLM_TENSOR_PARALLEL_SIZE:-4}" \
    --max-model-len 32768 \
    --dtype bfloat16 \
    --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"
