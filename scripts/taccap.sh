#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config_file="${TACCAP_CONFIG_FILE:-$project_dir/config/taccap.env}"
if [[ -f "$config_file" ]]; then
    # shellcheck disable=SC1090
    source "$config_file"
fi
if [[ -n "${TACCAP_ENV_SCRIPT:-}" ]]; then
    [[ -f "$TACCAP_ENV_SCRIPT" ]] || {
        echo "SDK environment script not found: $TACCAP_ENV_SCRIPT" >&2
        exit 1
    }
    # shellcheck disable=SC1090
    source "$TACCAP_ENV_SCRIPT" >/dev/null
fi
if [[ -n "${TACCAP_PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$TACCAP_PYTHONPATH"
else
    # The device service is self-contained.  Do not let a caller's ROS or
    # conda PYTHONPATH leak host modules into the bundled interpreter.
    unset PYTHONPATH
fi
unset PYTHONHOME PYTHONUSERBASE
export PYTHONNOUSERSITE=1
if [[ -n "${TACCAP_LD_LIBRARY_PATH:-}" ]]; then
    export LD_LIBRARY_PATH="$TACCAP_LD_LIBRARY_PATH"
else
    # Never inherit ROS/conda/old-SDK libraries into the managed service.  An
    # empty value means the native extension must use the target's default
    # system loader paths.
    unset LD_LIBRARY_PATH
fi

python_bin="${TACCAP_PYTHON:-}"
bind_host="${TACCAP_BIND_HOST:-0.0.0.0}"
port="${TACCAP_PORT:-8765}"
ffmpeg_bin="${TACCAP_FFMPEG:-/usr/bin/ffmpeg}"
pid_file="${TACCAP_PID_FILE:-$project_dir/taccap.pid}"
log_dir_value="${TACCAP_LOG_DIR:-.log}"
if [[ "$log_dir_value" = /* ]]; then
    log_dir="$log_dir_value"
else
    log_dir="$project_dir/$log_dir_value"
fi
latest_log="$log_dir/latest.log"
service_name="taccap-websocket.service"

case "$bind_host" in
    0.0.0.0|::) health_host="127.0.0.1" ;;
    *) health_host="$bind_host" ;;
esac
base_url="http://$health_host:$port"

usage() {
    cat <<'USAGE'
Usage: ./taccap.sh COMMAND

Commands:
  run       Run the server in the foreground (used by systemd)
  start     Start the service
  stop      Stop the service
  restart   Restart the service
  status    Show process and API status
  health    Query health and camera APIs
  doctor    Check Python SDKs and attached devices
  logs      Follow service logs
USAGE
}

has_user_service() {
    systemctl --user cat "$service_name" >/dev/null 2>&1
}

check_runtime() {
    if [[ -z "$python_bin" ]]; then
        echo "TACCAP_PYTHON is not configured; install a bundle with ./deploy.sh" >&2
        return 1
    fi
    if ! command -v "$python_bin" >/dev/null 2>&1 && [[ ! -x "$python_bin" ]]; then
        echo "Python executable not found: $python_bin" >&2
        return 1
    fi
    if [[ ! -x "$ffmpeg_bin" ]]; then
        echo "ffmpeg executable not found: $ffmpeg_bin" >&2
        return 1
    fi
}

run_server() {
    check_runtime
    mkdir -p "$log_dir"
    local timestamp run_log
    timestamp="$(date '+%Y%m%d-%H%M%S')"
    run_log="$log_dir/taccap_${timestamp}_$$.log"
    ln -sfn "$(basename "$run_log")" "$latest_log"
    exec >>"$run_log" 2>&1
    echo "$(date --iso-8601=seconds) starting TacCap service"
    echo "listen=$bind_host:$port python=$python_bin"
    cd "$project_dir"
    exec "$python_bin" -u src/taccap_websocket/server.py \
        --host "$bind_host" \
        --port "$port" \
        --ffmpeg "$ffmpeg_bin"
}

wait_until_ready() {
    local pid="${1:-}"
    local attempt
    for attempt in {1..90}; do
        if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
            echo "service failed to start; recent log:" >&2
            tail -n 80 "$latest_log" >&2 || true
            return 1
        fi
        if curl --noproxy '*' --fail --silent --max-time 1 \
            "$base_url/api/health" >/dev/null; then
            echo "TacCap service ready: http://$bind_host:$port"
            return 0
        fi
        sleep 0.5
    done
    echo "service process is alive but the health API is not ready" >&2
    return 1
}

start_service() {
    if has_user_service; then
        systemctl --user start "$service_name"
        wait_until_ready
        return
    fi
    if [[ -s "$pid_file" ]]; then
        local old_pid
        old_pid="$(<"$pid_file")"
        if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
            echo "TacCap service already running (PID $old_pid)"
            return
        fi
    fi
    cd "$project_dir"
    nohup "$project_dir/scripts/taccap.sh" run >/dev/null 2>&1 &
    local new_pid=$!
    printf '%s\n' "$new_pid" >"$pid_file"
    wait_until_ready "$new_pid"
    echo "PID: $new_pid"
    echo "Log: $latest_log"
}

stop_service() {
    if has_user_service; then
        systemctl --user stop "$service_name"
        echo "TacCap service stopped"
        return
    fi
    if [[ ! -s "$pid_file" ]]; then
        echo "TacCap service is not running"
        return
    fi
    local pid
    pid="$(<"$pid_file")"
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        echo "TacCap service is not running (removed stale PID file)"
        return
    fi
    kill -TERM "$pid"
    local attempt
    for attempt in {1..50}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file"
            echo "TacCap service stopped"
            return
        fi
        sleep 0.2
    done
    echo "service is still stopping (PID $pid)" >&2
    return 1
}

show_health() {
    curl --noproxy '*' --fail --silent --show-error --max-time 5 \
        "$base_url/api/health"
    printf '\n'
    curl --noproxy '*' --fail --silent --show-error --max-time 10 \
        "$base_url/api/cameras"
    printf '\n'
}

show_status() {
    if has_user_service; then
        printf 'systemd: %s\n' "$(systemctl --user is-active "$service_name" 2>/dev/null || true)"
    elif [[ -s "$pid_file" ]]; then
        local pid
        pid="$(<"$pid_file")"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            echo "process: running (PID $pid)"
        else
            echo "process: stopped (stale PID file)"
        fi
    else
        echo "process: stopped"
    fi
    echo "listen: $bind_host:$port"
    show_health
}

doctor() {
    local failed=0
    local command_name
    for command_name in "$python_bin" "$ffmpeg_bin" curl; do
        if command -v "$command_name" >/dev/null 2>&1 || [[ -x "$command_name" ]]; then
            printf 'OK   command            %s\n' "$command_name"
        else
            printf 'FAIL command not found  %s\n' "$command_name"
            failed=1
        fi
    done
    if "$python_bin" -c 'import xense.taccap' >/dev/null 2>&1; then
        echo "OK   xense.taccap       importable"
    else
        echo "FAIL xense.taccap       Python SDK import failed"
        failed=1
    fi
    if "$python_bin" -c 'import xensesdk' >/dev/null 2>&1; then
        echo "OK   xensesdk           importable"
    else
        echo "FAIL xensesdk           Python SDK import failed"
        failed=1
    fi
    local serial_count camera_count
    serial_count="$(find /dev/serial/by-id -maxdepth 1 -type l 2>/dev/null | wc -l)"
    camera_count="$(find /dev/v4l/by-id -maxdepth 1 -type l 2>/dev/null | wc -l)"
    printf 'INFO serial devices    %s\n' "$serial_count"
    printf 'INFO V4L2 by-id links   %s\n' "$camera_count"
    if command -v v4l2-ctl >/dev/null 2>&1; then
        v4l2-ctl --list-devices 2>/dev/null || true
    fi
    ((failed == 0))
}

show_logs() {
    mkdir -p "$log_dir"
    if [[ ! -e "$latest_log" ]]; then
        echo "no runtime log yet: $log_dir" >&2
        exit 1
    fi
    exec tail -F "$latest_log"
}

command_name="${1:-status}"
case "$command_name" in
    run) run_server ;;
    start) start_service ;;
    stop) stop_service ;;
    restart)
        stop_service
        start_service
        ;;
    status) show_status ;;
    health) show_health ;;
    doctor) doctor ;;
    logs) show_logs ;;
    -h|--help|help) usage ;;
    *)
        echo "unknown command: $command_name" >&2
        usage >&2
        exit 2
        ;;
esac
