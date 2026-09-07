#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pid_file="$project_dir/taccap.pid"

if [[ ! -s "$pid_file" ]]; then
    echo "taccap service is not running (no PID file)"
    exit 0
fi
pid="$(<"$pid_file")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "taccap service is not running (stale PID file)"
    rm -f "$pid_file"
    exit 0
fi

kill -TERM "$pid"
for _ in {1..50}; do
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        echo "taccap service stopped"
        exit 0
    fi
    sleep 0.2
done
echo "taccap service is still stopping (PID $pid); inspect taccap.log" >&2
exit 1

