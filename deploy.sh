#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: ./deploy.sh USER@HOST [options]

Deploy the complete offline release to a new target over SSH.
The target does not need Python, pip, uv, Git or network access.

Options:
  --bundle DIR       Offline bundle from ./bundle.sh (default: ./offline)
  --install-dir DIR Target installation directory (default: ~/taccap-websocket)
  --no-start         Install only; do not start the service
  -h, --help         Show this help

Examples:
  ./deploy.sh guest@10.192.1.4
  ./deploy.sh guest@10.192.1.4 --bundle ./offline --install-dir /home/guest/taccap-websocket
USAGE
}

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
target=""
bundle_dir="$project_dir/offline"
install_dir=""
no_start=0

if (($#)) && [[ "$1" != -* ]]; then
    target="$1"
    shift
fi
while (($#)); do
    case "$1" in
        --bundle)
            (($# >= 2)) || { echo "missing argument for --bundle" >&2; exit 2; }
            bundle_dir="$2"; shift 2 ;;
        --install-dir)
            (($# >= 2)) || { echo "missing argument for --install-dir" >&2; exit 2; }
            install_dir="$2"; shift 2 ;;
        --no-start) no_start=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ -n "$target" ]] || { usage >&2; exit 2; }
[[ -d "$bundle_dir/python" && -f "$bundle_dir/site-packages.tar.gz" ]] || {
    echo "invalid offline bundle: $bundle_dir (run ./bundle.sh first)" >&2; exit 1;
}
[[ -f "$bundle_dir/manifest.txt" ]] || {
    echo "offline bundle is missing manifest.txt: $bundle_dir" >&2; exit 1;
}

shell_quote() {
    local value="$1"
    printf "'%s'" "${value//\'/\'\\\'}"
}

commit="$(git -C "$project_dir" rev-parse --short HEAD 2>/dev/null || printf 'working-tree')"
if ! git -C "$project_dir" diff --quiet --no-ext-diff 2>/dev/null ||
   ! git -C "$project_dir" diff --cached --quiet --no-ext-diff 2>/dev/null; then
    commit="$commit (working-tree changes)"
fi

remote_tmp="/tmp/taccap-deploy-$USER-$RANDOM"
remote_tmp_q="$(shell_quote "$remote_tmp")"
ssh_control_dir="$(mktemp -d /tmp/taccap-deploy-ssh.XXXXXX)"
ssh_control_path="$ssh_control_dir/control"
ssh_command=(ssh -o ControlMaster=auto -o ControlPersist=60 -o "ControlPath=$ssh_control_path")
remote_ssh() { "${ssh_command[@]}" "$target" "$@"; }
cleanup() {
    remote_ssh "rm -rf -- $remote_tmp_q" >/dev/null 2>&1 || true
    "${ssh_command[@]}" -O exit "$target" >/dev/null 2>&1 || true
    rm -rf -- "$ssh_control_dir"
}
trap cleanup EXIT

echo "Deploying release $commit to $target"
remote_ssh "umask 077; mkdir -p -- $remote_tmp_q/source $remote_tmp_q/offline"

tar \
    --exclude='./.git' --exclude='./.venv' --exclude='./.runtime' \
    --exclude='./offline' --exclude='./vendor' --exclude='./.log' \
    --exclude='./config/taccap.env' --exclude='./config/devices.json' \
    --exclude='./__pycache__' \
    -czf - -C "$project_dir" . |
    remote_ssh "tar -xzf - -C $remote_tmp_q/source"

offline_files=(python site-packages.tar.gz manifest.txt)
[[ -f "$bundle_dir/runtime-libs.tar.gz" ]] && offline_files+=(runtime-libs.tar.gz)
tar -czf - -C "$bundle_dir" "${offline_files[@]}" |
    remote_ssh "tar -xzf - -C $remote_tmp_q/offline"
remote_ssh "printf '%s\n' $(shell_quote "$commit") > $remote_tmp_q/source/.release"

remote_cmd="set -e; bash $remote_tmp_q/source/scripts/install_target.sh --offline-dir $remote_tmp_q/offline"
if [[ -n "$install_dir" ]]; then
    remote_cmd+=" --install-dir $(shell_quote "$install_dir")"
fi
if ((no_start)); then
    remote_cmd+=" --no-start"
fi
remote_cmd+="; rm -rf -- $remote_tmp_q"
remote_ssh "$remote_cmd"

"${ssh_command[@]}" -O exit "$target" >/dev/null 2>&1 || true
rm -rf -- "$ssh_control_dir"
trap - EXIT
echo "Deployment completed: $target"
