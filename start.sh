#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pid_file="$project_dir/taccap.pid"
log_file="$project_dir/taccap.log"

if [[ -s "$pid_file" ]]; then
    old_pid="$(<"$pid_file")"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "taccap service already running (PID $old_pid)"
        exit 0
    fi
fi

source /home/guest/activate_taccap312
cd "$project_dir"
nohup "$TACCAP_PYTHON" -u server.py --host 127.0.0.1 --port 8765 >>"$log_file" 2>&1 &
new_pid=$!
printf '%s\n' "$new_pid" >"$pid_file"

for _ in {1..90}; do
    if ! kill -0 "$new_pid" 2>/dev/null; then
        echo "taccap service failed to start; recent log:" >&2
        tail -n 80 "$log_file" >&2 || true
        exit 1
    fi
    if curl --noproxy '*' --fail --silent --show-error --max-time 1 \
        http://127.0.0.1:8765/api/health >/dev/null; then
        echo "taccap service started (PID $new_pid)"
        echo "URL: http://127.0.0.1:8765 (use an SSH tunnel from the operator PC)"
        echo "Log: $log_file"
        exit 0
    fi
    sleep 0.5
done

echo "taccap process is alive but health endpoint is not ready; recent log:" >&2
tail -n 80 "$log_file" >&2 || true
exit 1

