#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: deploy.sh USER@HOST [options]

Upload this checkout to a target machine and run install.sh there.

Options:
  --install-dir DIR       Remote installation directory
  --python PATH           Base Python executable on the remote device
  --env-script PATH       SDK environment script on the remote device
  --wheel-dir DIR         Local directory containing private SDK wheels
  --no-deps               Do not create a virtualenv or install Python packages
  --install-system-deps   Install Ubuntu packages with apt (sudo required)
  --enable-systemd        Install and enable a user systemd service
  --no-start              Deploy without starting the service
  -h, --help              Show this help
USAGE
}

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target=""
install_dir=""
base_python=""
wheel_dir=""
env_script=""
with_deps=1
install_system_deps=0
enable_systemd=0
no_start=0

if (($# == 1)) && [[ "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 0
fi
if (($#)) && [[ "$1" != "-h" && "$1" != "--help" ]]; then
    target="$1"
    shift
fi
[[ -n "$target" ]] || { usage >&2; exit 2; }

while (($#)); do
    case "$1" in
        --install-dir)
            (($# >= 2)) || { echo "missing argument for --install-dir" >&2; exit 2; }
            install_dir="$2"
            shift 2
            ;;
        --python)
            (($# >= 2)) || { echo "missing argument for --python" >&2; exit 2; }
            base_python="$2"
            shift 2
            ;;
        --env-script)
            (($# >= 2)) || { echo "missing argument for --env-script" >&2; exit 2; }
            env_script="$2"
            shift 2
            ;;
        --wheel-dir)
            (($# >= 2)) || { echo "missing argument for --wheel-dir" >&2; exit 2; }
            wheel_dir="$2"
            shift 2
            ;;
        --no-deps)
            with_deps=0
            shift
            ;;
        --install-system-deps)
            install_system_deps=1
            shift
            ;;
        --enable-systemd)
            enable_systemd=1
            shift
            ;;
        --no-start)
            no_start=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

shell_quote() {
    local value="$1"
    printf "'%s'" "${value//\'/\'\\\'}"
}

commit="$(git -C "$project_dir" rev-parse --short HEAD 2>/dev/null || printf 'working-tree')"
if ! git -C "$project_dir" diff --quiet --no-ext-diff 2>/dev/null ||
   ! git -C "$project_dir" diff --cached --quiet --no-ext-diff 2>/dev/null; then
    commit="$commit (working-tree changes)"
fi

remote_tmp="/tmp/taccap-websocket-deploy-$USER-$RANDOM"
remote_tmp_q="$(shell_quote "$remote_tmp")"
ssh_control_dir="$(mktemp -d /tmp/taccap-deploy-ssh.XXXXXX)"
ssh_control_path="$ssh_control_dir/control"
ssh_command=(
    ssh
    -o ControlMaster=auto
    -o ControlPersist=60
    -o "ControlPath=$ssh_control_path"
)
remote_ssh() {
    "${ssh_command[@]}" "$target" "$@"
}
cleanup() {
    remote_ssh "rm -rf -- $remote_tmp_q" >/dev/null 2>&1 || true
    "${ssh_command[@]}" -O exit "$target" >/dev/null 2>&1 || true
    rm -rf -- "$ssh_control_dir"
}
trap cleanup EXIT

echo "Uploading commit: $commit"
remote_ssh "umask 077; mkdir -p -- $remote_tmp_q"
tar \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./config/taccap.env' \
    --exclude='./config/devices.json' \
    --exclude='./taccap.pid' \
    --exclude='./taccap.log' \
    --exclude='./*.log' \
    --exclude='./.log' \
    --exclude='./__pycache__' \
    -czf - -C "$project_dir" . |
    remote_ssh "tar -xzf - -C $remote_tmp_q"
remote_ssh "printf '%s\n' $(shell_quote "$commit") > $remote_tmp_q/.release"

if [[ -n "$wheel_dir" ]]; then
    [[ -d "$wheel_dir" ]] || { echo "wheel directory not found: $wheel_dir" >&2; exit 1; }
    remote_ssh "mkdir -p -- $remote_tmp_q/vendor-wheels"
    tar -czf - -C "$wheel_dir" . |
        remote_ssh "tar -xzf - -C $remote_tmp_q/vendor-wheels"
fi

remote_cmd="set -e; bash $remote_tmp_q/install.sh"
if [[ -n "$install_dir" ]]; then
    remote_cmd+=" --install-dir $(shell_quote "$install_dir")"
fi
if [[ -n "$base_python" ]]; then
    remote_cmd+=" --python $(shell_quote "$base_python")"
fi
if [[ -n "$env_script" ]]; then
    remote_cmd+=" --env-script $(shell_quote "$env_script")"
fi
if [[ -n "$wheel_dir" ]]; then
    remote_cmd+=" --wheel-dir $(shell_quote "$remote_tmp/vendor-wheels")"
fi
if ((with_deps)); then
    remote_cmd+=" --with-deps"
fi
if ((install_system_deps)); then
    remote_cmd+=" --install-system-deps"
fi
if ((enable_systemd)); then
    remote_cmd+=" --enable-systemd"
fi
if ((no_start)); then
    remote_cmd+=" --no-start"
fi
remote_cmd+="; rm -rf -- $remote_tmp_q"
if ((install_system_deps)); then
    "${ssh_command[@]}" -tt "$target" "$remote_cmd"
else
    remote_ssh "$remote_cmd"
fi
"${ssh_command[@]}" -O exit "$target" >/dev/null 2>&1 || true
rm -rf -- "$ssh_control_dir"
trap - EXIT
echo "Deployment completed on $target"
