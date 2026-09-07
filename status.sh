#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pid_file="$project_dir/taccap.pid"
if [[ -s "$pid_file" ]]; then
    pid="$(<"$pid_file")"
else
    pid=""
fi
if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "process: running (PID $pid)"
else
    echo "process: stopped"
fi
curl --noproxy '*' --fail --silent --show-error \
    http://127.0.0.1:8765/api/health
echo
curl --noproxy '*' --fail --silent --show-error \
    http://127.0.0.1:8765/api/cameras
echo

