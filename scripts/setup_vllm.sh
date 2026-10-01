#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
venv_dir="${VLLM_ENV_DIR:-${repo_root}/.venv-vllm}"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

uv venv --python 3.12 --seed "${venv_dir}"
uv pip install --python "${venv_dir}/bin/python" --torch-backend=auto -r "${repo_root}/requirements-vllm.txt"

echo "vLLM environment created at ${venv_dir}."
echo "No model server was started and no model weights were requested."
