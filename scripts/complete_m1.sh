#!/usr/bin/env bash
set -euo pipefail
export AETERNITAS_AUTH=chatgpt
# HTTPX does not interpret 127.* as a loopback bypass rule.
export NO_PROXY="localhost,127.0.0.1,::1,${NO_PROXY:-${no_proxy:-}}"
export no_proxy="$NO_PROXY"
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
project_python="$project_dir/.venv/bin/python"
if [[ ! -x "$project_python" ]]; then
    echo "找不到项目 .venv/bin/python；请回到原 WSL 项目，不要在下载目录另建环境。" >&2
    exit 1
fi
"$project_python" -m pip install -r requirements.txt
"$project_python" -m pytest -q
"$project_python" aeternitas.py configure --provider chatgpt
"$project_python" aeternitas.py login --if-needed
"$project_python" aeternitas.py doctor
"$project_python" aeternitas.py run-local AAPL --refresh
