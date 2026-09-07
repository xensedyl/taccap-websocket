#!/usr/bin/env bash
set -euo pipefail

# Internal installer. It is called by ../deploy.sh on the target machine.
# The target is offline: the bundle contains Python and every Python wheel.

usage() {
    cat <<'USAGE'
Usage: install_target.sh --offline-dir DIR [options]

Internal offline installer (normally invoked by deploy.sh).

Options:
  --offline-dir DIR   Bundle containing python/, wheels/ and manifest.txt
  --install-dir DIR   Installation directory (default: $HOME/taccap-websocket)
  --no-start          Install without starting the service
  -h, --help          Show this help
USAGE
}

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
install_dir="${HOME}/taccap-websocket"
offline_dir=""
no_start=0

while (($#)); do
    case "$1" in
        --offline-dir)
            (($# >= 2)) || { echo "missing argument for --offline-dir" >&2; exit 2; }
            offline_dir="$2"; shift 2 ;;
        --install-dir)
            (($# >= 2)) || { echo "missing argument for --install-dir" >&2; exit 2; }
            install_dir="$2"; shift 2 ;;
        --no-start) no_start=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -n "$offline_dir" ]] || { echo "--offline-dir is required" >&2; exit 2; }
[[ "$install_dir" = /* ]] || { echo "--install-dir must be absolute: $install_dir" >&2; exit 2; }
case "$install_dir" in /|/home|/root|/tmp)
    echo "refusing unsafe installation directory: $install_dir" >&2; exit 2;;
esac
[[ -d "$offline_dir/python" && -d "$offline_dir/wheels" ]] || {
    echo "offline bundle must contain python/ and wheels/: $offline_dir" >&2; exit 1;
}
[[ -f "$offline_dir/manifest.txt" ]] || {
    echo "offline bundle is missing manifest.txt: $offline_dir" >&2; exit 1;
}
command -v curl >/dev/null 2>&1 || { echo "curl is required on target" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required on target" >&2; exit 1; }

echo "Verifying offline bundle: $offline_dir"
awk 'BEGIN { hashes=0 } /^sha256:/ { hashes=1; next } hashes && NF { print }' \
    "$offline_dir/manifest.txt" | (cd "$offline_dir" && sha256sum -c -)

mkdir -p "$install_dir/.runtime"
runtime_stage="$(mktemp -d "$install_dir/.runtime/.python.XXXXXX")"
trap 'rm -rf -- "$runtime_stage"' EXIT
tar -cf - -C "$offline_dir/python" . | tar -xf - -C "$runtime_stage"
python_home="$install_dir/.runtime/python"
if [[ -e "$python_home" ]]; then
    [[ -f "$install_dir/.runtime/.managed-by-taccap" ]] || {
        echo "refusing to replace unmanaged runtime: $python_home" >&2; exit 1;
    }
    rm -rf -- "$python_home"
fi
mv -- "$runtime_stage" "$python_home"
trap - EXIT
: >"$install_dir/.runtime/.managed-by-taccap"
base_python="$(find "$python_home" \( -type f -o -type l \) -path '*/bin/python3.12' -print -quit)"
[[ -x "$base_python" ]] || { echo "offline bundle has no Python 3.12" >&2; exit 1; }

mkdir -p "$install_dir"
saved_config_dir="$(mktemp -d)"
for config_name in taccap.env devices.json; do
    [[ -f "$install_dir/config/$config_name" ]] && cp -p "$install_dir/config/$config_name" "$saved_config_dir/$config_name"
done

tar \
    --exclude='./.git' --exclude='./.venv' --exclude='./.runtime' \
    --exclude='./offline' --exclude='./vendor' \
    --exclude='./config/taccap.env' --exclude='./config/devices.json' \
    --exclude='./.log' --exclude='./__pycache__' \
    -cf - -C "$project_dir" . | tar -xf - -C "$install_dir"
mkdir -p "$install_dir/config" "$install_dir/.log"
for saved_config in "$saved_config_dir"/*; do
    [[ -f "$saved_config" ]] || continue
    cp -p "$saved_config" "$install_dir/config/$(basename "$saved_config")"
done
rm -rf -- "$saved_config_dir"

[[ -f "$install_dir/config/taccap.env" ]] || cp "$install_dir/config/taccap.env.example" "$install_dir/config/taccap.env"
python_bin="$install_dir/.venv/bin/python"
"$base_python" -m venv "$install_dir/.venv"
"$python_bin" -m pip install --no-index --find-links "$offline_dir/wheels" -r "$install_dir/requirements.txt"

set_config_value() {
    local name="$1" value="$2" tmp
    tmp="$(mktemp)"
    awk -v name="$name" -v value="$value" '
        BEGIN { replaced=0 }
        $0 ~ ("^" name "=") { print name "=" value; replaced=1; next }
        { print }
        END { if (!replaced) print name "=" value }
    ' "$install_dir/config/taccap.env" >"$tmp"
    mv -- "$tmp" "$install_dir/config/taccap.env"
}
set_config_value TACCAP_PYTHON "$python_bin"
set_config_value TACCAP_ENV_SCRIPT ""
set_config_value TACCAP_PYTHON_HOME "$install_dir/.runtime/python"

chmod +x "$install_dir/scripts/taccap.sh"
for legacy_file in server.py tactile_worker.py client.py taccap.sh install.sh bundle_offline.sh; do
    rm -f -- "$install_dir/$legacy_file"
done
[[ -f "$install_dir/.release" ]] && echo "Release: $(<"$install_dir/.release")"
echo "Installed project: $install_dir"

start_directly=1
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_file="$unit_dir/taccap-websocket.service"
mkdir -p "$unit_dir"
cat >"$unit_file" <<UNIT
[Unit]
Description=TacCap WebSocket/HTTP camera and gripper service
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$install_dir
EnvironmentFile=-$install_dir/config/taccap.env
ExecStart=$install_dir/scripts/taccap.sh run
Restart=on-failure
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=15
NoNewPrivileges=true

[Install]
WantedBy=default.target
UNIT
if command -v systemctl >/dev/null 2>&1 &&
   systemctl --user daemon-reload >/dev/null 2>&1 &&
   systemctl --user enable taccap-websocket.service >/dev/null 2>&1; then
    start_directly=0
    if ((no_start == 0)); then
        systemctl --user restart taccap-websocket.service
    fi
    echo "User systemd unit: $unit_file"
else
    echo "warning: user systemd unavailable; using scripts/taccap.sh" >&2
fi
if ((no_start == 0 && start_directly == 1)); then
    "$install_dir/scripts/taccap.sh" start
elif ((no_start == 1)); then
    echo "Service not started (--no-start)"
fi
echo "Installation complete. Run '$install_dir/scripts/taccap.sh doctor' to verify devices."
