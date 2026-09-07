#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: install.sh [options]

Install the TacCap service on the current machine.

Options:
  --install-dir DIR       Installation directory (default: $HOME/taccap-websocket)
  --python PATH           Base Python executable for the virtualenv
  --bootstrap-python      Install a private Python 3.12 with uv when needed
  --env-script PATH       SDK environment script sourced before startup
  --wheel-dir DIR         Directory containing private SDK wheels
  --offline-dir DIR       Offline bundle containing python/ and wheels/
  --with-deps             Create .venv and install requirements.txt
  --install-system-deps   Install Ubuntu packages with apt (sudo required)
  --enable-systemd        Install and enable a user systemd service
  --no-start              Install only; do not start or enable the service
  -h, --help              Show this help
USAGE
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install_dir="${HOME}/taccap-websocket"
base_python="${TACCAP_BASE_PYTHON:-python3}"
python_explicit=0
if [[ -n "${TACCAP_BASE_PYTHON:-}" ]]; then
    python_explicit=1
fi
wheel_dir="${TACCAP_WHEEL_DIR:-}"
offline_dir="${TACCAP_OFFLINE_DIR:-}"
offline_mode=0
bootstrap_python=0
env_script=""
env_script_explicit=0
with_deps=0
install_system_deps=0
enable_systemd=0
no_start=0

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
            python_explicit=1
            shift 2
            ;;
        --bootstrap-python)
            bootstrap_python=1
            shift
            ;;
        --env-script)
            (($# >= 2)) || { echo "missing argument for --env-script" >&2; exit 2; }
            env_script="$2"
            env_script_explicit=1
            shift 2
            ;;
        --wheel-dir)
            (($# >= 2)) || { echo "missing argument for --wheel-dir" >&2; exit 2; }
            wheel_dir="$2"
            shift 2
            ;;
        --offline-dir)
            (($# >= 2)) || { echo "missing argument for --offline-dir" >&2; exit 2; }
            offline_dir="$2"
            shift 2
            ;;
        --with-deps)
            with_deps=1
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

[[ "$install_dir" = /* ]] || {
    echo "--install-dir must be an absolute path: $install_dir" >&2
    exit 2
}
case "$install_dir" in
    /|/home|/root|/tmp)
        echo "refusing unsafe installation directory: $install_dir" >&2
        exit 2
        ;;
esac

if ((install_system_deps)); then
    if [[ -n "$offline_dir" ]]; then
        echo "--install-system-deps cannot be used with --offline-dir" >&2
        echo "Install the required Ubuntu packages before disconnecting the target." >&2
        exit 2
    fi
    command -v sudo >/dev/null 2>&1 || {
        echo "sudo is required for --install-system-deps" >&2
        exit 1
    }
    sudo apt-get update
    sudo apt-get install -y \
        curl ffmpeg libusb-1.0-0 libusb-1.0-0-dev \
        python3 python3-pip python3-venv rsync v4l-utils
fi

if [[ -n "$offline_dir" ]]; then
    if ((bootstrap_python)); then
        echo "--bootstrap-python cannot be used with --offline-dir" >&2
        exit 2
    fi
    if ((python_explicit)); then
        echo "--python cannot be used with --offline-dir; the bundle supplies Python 3.12" >&2
        exit 2
    fi
    if ((with_deps == 0)); then
        echo "--no-deps cannot be used with --offline-dir; the bundle must populate .venv" >&2
        exit 2
    fi
    if ((env_script_explicit)); then
        echo "--env-script cannot be used with --offline-dir; configure target libraries in the image" >&2
        exit 2
    fi
    [[ -d "$offline_dir/python" && -d "$offline_dir/wheels" ]] || {
        echo "offline bundle must contain python/ and wheels/: $offline_dir" >&2
        exit 1
    }
    [[ -f "$offline_dir/manifest.txt" ]] || {
        echo "offline bundle is missing manifest.txt: $offline_dir" >&2
        exit 1
    }
    offline_mode=1
    # The target may not have the installation directory yet.  Create only
    # the managed runtime parent before staging the copied interpreter.
    mkdir -p "$install_dir/.runtime"
    python_home="$install_dir/.runtime/python"
    # The managed interpreter is copied rather than installed by uv on the
    # target.  This is what makes a deployment work without internet access or
    # a pre-existing Python 3.12 installation.
    runtime_stage="$(mktemp -d "$install_dir/.runtime/.python.XXXXXX")"
    tar -cf - -C "$offline_dir/python" . | tar -xf - -C "$runtime_stage"
    if [[ -e "$python_home" ]]; then
        # Only replace a runtime previously managed by this installer.  Do not
        # remove an administrator-owned directory by accident.
        if [[ ! -f "$install_dir/.runtime/.managed-by-taccap" ]]; then
            echo "refusing to replace unmanaged runtime: $python_home" >&2
            rm -rf -- "$runtime_stage"
            exit 1
        fi
        rm -rf -- "$python_home"
    fi
    mv -- "$runtime_stage" "$python_home"
    : >"$install_dir/.runtime/.managed-by-taccap"
    base_python="$(find "$python_home" \( -type f -o -type l \) \
        -path '*/bin/python3.12' -print -quit)"
    [[ -x "$base_python" ]] || {
        echo "offline bundle contains no executable Python 3.12 under $python_home" >&2
        exit 1
    }
    wheel_dir="$offline_dir/wheels"
elif ((bootstrap_python)); then
    command -v uv >/dev/null 2>&1 || {
        echo "uv is required for --bootstrap-python; install it from https://astral.sh/uv" >&2
        exit 1
    }
    python_home="$install_dir/.runtime/python"
    uv python install 3.12 --install-dir "$python_home"
    base_python="$(find "$python_home" \( -type f -o -type l \) \
        -path '*/bin/python3.12' -print -quit)"
    [[ -x "$base_python" ]] || {
        echo "uv installed Python but no executable was found under $python_home" >&2
        exit 1
    }
elif ! command -v "$base_python" >/dev/null 2>&1 && [[ ! -x "$base_python" ]]; then
    echo "Python executable not found: $base_python (use --bootstrap-python or --python PATH)" >&2
    exit 1
fi
if ! "$base_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python 3.10 or newer is required: $base_python" >&2
    echo "Use --python /path/to/python3.12 or install a newer Python." >&2
    exit 1
fi
command -v curl >/dev/null 2>&1 || {
    echo "curl is required (or use --install-system-deps)" >&2
    exit 1
}
command -v ffmpeg >/dev/null 2>&1 || {
    echo "ffmpeg is required (or use --install-system-deps)" >&2
    exit 1
}

mkdir -p "$install_dir"
if [[ "$script_dir" != "$install_dir" ]]; then
    saved_config_dir="$(mktemp -d)"
    for config_name in taccap.env devices.json; do
        if [[ -f "$install_dir/config/$config_name" ]]; then
            cp -p "$install_dir/config/$config_name" "$saved_config_dir/$config_name"
        fi
    done
    tar \
        --exclude='./.git' \
        --exclude='./.venv' \
        --exclude='./.runtime' \
        --exclude='./offline' \
        --exclude='./vendor' \
        --exclude='./config/taccap.env' \
        --exclude='./config/devices.json' \
        --exclude='./taccap.pid' \
        --exclude='./taccap.log' \
        --exclude='./.log' \
        --exclude='./__pycache__' \
        -cf - -C "$script_dir" . |
        tar -xf - -C "$install_dir"
    mkdir -p "$install_dir/config"
    for saved_config in "$saved_config_dir"/*; do
        [[ -f "$saved_config" ]] || continue
        cp -p "$saved_config" "$install_dir/config/$(basename "$saved_config")"
    done
    rm -rf -- "$saved_config_dir"
fi

mkdir -p "$install_dir/config"
mkdir -p "$install_dir/.log"
if [[ -f "$install_dir/taccap.log" ]]; then
    mv "$install_dir/taccap.log" \
        "$install_dir/.log/taccap_legacy_$(date '+%Y%m%d-%H%M%S').log"
fi
if [[ ! -f "$install_dir/config/taccap.env" ]]; then
    cp "$install_dir/config/taccap.env.example" "$install_dir/config/taccap.env"
fi

if ((python_explicit)) && ((with_deps == 0)); then
    tmp_config="$(mktemp)"
    awk -v value="$base_python" '
        BEGIN { replaced = 0 }
        /^TACCAP_PYTHON=/ {
            print "TACCAP_PYTHON=" value
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) print "TACCAP_PYTHON=" value
        }
    ' "$install_dir/config/taccap.env" >"$tmp_config"
    mv "$tmp_config" "$install_dir/config/taccap.env"
fi

if ((env_script_explicit)); then
    [[ -f "$env_script" ]] || {
        echo "SDK environment script not found: $env_script" >&2
        exit 1
    }
    tmp_config="$(mktemp)"
    awk -v value="$env_script" '
        BEGIN { replaced = 0 }
        /^TACCAP_ENV_SCRIPT=/ {
            print "TACCAP_ENV_SCRIPT=" value
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) print "TACCAP_ENV_SCRIPT=" value
        }
    ' "$install_dir/config/taccap.env" >"$tmp_config"
    mv "$tmp_config" "$install_dir/config/taccap.env"
fi

if ((with_deps)); then
    "$base_python" -m venv "$install_dir/.venv"
    if [[ -n "$wheel_dir" ]]; then
        [[ -d "$wheel_dir" ]] || {
            echo "wheel directory not found: $wheel_dir" >&2
            exit 1
        }
        shopt -s nullglob
        private_wheels=(
            "$wheel_dir"/xensesdk-*.whl
            "$wheel_dir"/taccap_gripper-*.whl
        )
        shopt -u nullglob
        ((${#private_wheels[@]} >= 2)) || {
            echo "wheel directory must contain xensesdk and taccap-gripper wheels: $wheel_dir" >&2
            exit 1
        }
    fi
    if ((offline_mode)); then
        # Never let pip consult an index in offline mode.  This is deliberately
        # one resolver invocation so transitive dependencies (including the
        # vendor-only cypack 0.1.2 wheel) are checked together.
        "$install_dir/.venv/bin/python" -m pip install \
            --no-index --find-links "$wheel_dir" \
            -r "$install_dir/requirements.txt"
    elif [[ -n "$wheel_dir" ]]; then
        "$install_dir/.venv/bin/python" -m pip install \
            --find-links "$wheel_dir" -r "$install_dir/requirements.txt"
    else
        "$install_dir/.venv/bin/python" -m pip install -r "$install_dir/requirements.txt"
    fi
    python_bin="$install_dir/.venv/bin/python"
    tmp_config="$(mktemp)"
    awk -v value="$python_bin" '
        BEGIN { replaced = 0 }
        /^TACCAP_PYTHON=/ {
            print "TACCAP_PYTHON=" value
            replaced = 1
            next
        }
        { print }
        END {
            if (!replaced) print "TACCAP_PYTHON=" value
        }
    ' "$install_dir/config/taccap.env" >"$tmp_config"
    mv "$tmp_config" "$install_dir/config/taccap.env"
fi

chmod +x \
    "$install_dir/taccap.sh" \
    2>/dev/null || true

# Remove entry points used by releases before service management was
# consolidated into taccap.sh.
for legacy_script in run.sh start.sh stop.sh status.sh healthcheck.sh doctor.sh; do
    rm -f "$install_dir/$legacy_script"
done

if [[ -f "$install_dir/.release" ]]; then
    echo "Release: $(<"$install_dir/.release")"
fi
echo "Installed project: $install_dir"

start_directly=1
if ((enable_systemd)); then
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
ExecStart=$install_dir/taccap.sh run
Restart=on-failure
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=15
NoNewPrivileges=true

[Install]
WantedBy=default.target
UNIT
    if systemctl --user daemon-reload >/dev/null 2>&1 &&
       systemctl --user enable taccap-websocket.service >/dev/null 2>&1; then
        start_directly=0
        if ((no_start == 0)); then
            systemctl --user restart taccap-websocket.service
        fi
        echo "User systemd unit: $unit_file"
    else
        echo "warning: user systemd is unavailable; using taccap.sh start" >&2
    fi
fi

if ((no_start == 0 && start_directly == 1)); then
    "$install_dir/taccap.sh" start
elif ((no_start == 1)); then
    echo "Service not started (--no-start)"
fi

echo "Run '$install_dir/taccap.sh doctor' to verify SDKs and USB devices."
