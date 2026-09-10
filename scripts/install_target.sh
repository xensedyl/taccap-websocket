#!/usr/bin/env bash
set -euo pipefail

# Internal installer. It is called by ../deploy.sh on the target machine.
# The default bundle contains public site-packages plus TacCap source and a
# small offline build-tool wheelhouse.  The native TacCap extension is built
# here, against this machine's Ubuntu 20.04/glibc/OpenCV installation.

usage() {
    cat <<'USAGE'
Usage: install_target.sh --offline-dir DIR [options]

Internal offline installer (normally invoked by deploy.sh).

Options:
  --offline-dir DIR   Bundle containing python/, site-packages.tar.gz and manifest.txt
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
[[ -d "$offline_dir/python" && -f "$offline_dir/site-packages.tar.gz" ]] || {
    echo "offline bundle must contain python/ and site-packages.tar.gz: $offline_dir" >&2; exit 1;
}
if [[ -e "$offline_dir/runtime-libs.tar.gz" && ! -f "$offline_dir/runtime-libs.tar.gz" ]]; then
    echo "offline runtime-libs.tar.gz is not a regular file" >&2
    exit 1
fi
[[ -f "$offline_dir/manifest.txt" ]] || {
    echo "offline bundle is missing manifest.txt: $offline_dir" >&2; exit 1;
}
target_build=0
if [[ -f "$offline_dir/taccap-source.tar.gz" && -f "$offline_dir/build-wheels.tar.gz" ]]; then
    target_build=1
elif [[ -f "$offline_dir/taccap-source.tar.gz" || -f "$offline_dir/build-wheels.tar.gz" ]]; then
    echo "offline target-build bundle is incomplete: both taccap-source.tar.gz and build-wheels.tar.gz are required" >&2
    exit 1
fi
command -v curl >/dev/null 2>&1 || { echo "curl is required on target" >&2; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required on target" >&2; exit 1; }

echo "Verifying offline bundle: $offline_dir"
awk 'BEGIN { hashes=0 } /^sha256:/ { hashes=1; next } hashes && NF { print }' \
    "$offline_dir/manifest.txt" | (cd "$offline_dir" && sha256sum -c -)

# Preflight the native modules before stopping a currently working service or
# replacing its runtime.  A target build happens in this disposable directory
# and therefore leaves the existing installation untouched if compilation or
# import verification fails.
preflight_dir="$(mktemp -d /tmp/taccap-preflight.XXXXXX)"
target_build_root=""
preflight_cleanup() {
    rm -rf -- "$preflight_dir"
    [[ -z "$target_build_root" ]] || rm -rf -- "$target_build_root"
}
trap preflight_cleanup EXIT
mkdir -p "$preflight_dir/python" "$preflight_dir/site-packages" "$preflight_dir/lib"
tar -cf - -C "$offline_dir/python" . | tar -xf - -C "$preflight_dir/python"
tar -xzf "$offline_dir/site-packages.tar.gz" -C "$preflight_dir/site-packages"
if [[ -f "$offline_dir/runtime-libs.tar.gz" ]]; then
    tar -xzf "$offline_dir/runtime-libs.tar.gz" -C "$preflight_dir/lib"
fi
preflight_python="$(find "$preflight_dir/python" \( -type f -o -type l \) \
    -path '*/bin/python3.12' -print -quit)"
[[ -x "$preflight_python" ]] || {
    echo "offline bundle has no Python 3.12 executable for ABI preflight" >&2
    exit 1
}

target_wheel=""
target_build_python=""
if ((target_build)); then
    [[ -f "$offline_dir/fmt-headers.tar.gz" ]] || {
        echo "target-build bundle is missing fmt-headers.tar.gz" >&2
        exit 1
    }
    command -v c++ >/dev/null 2>&1 || {
        echo "target build requires a C++ compiler (c++)" >&2
        echo "Install build-essential on this Ubuntu 20.04 target." >&2
        exit 1
    }
    [[ -f /usr/include/opencv4/opencv2/core.hpp ]] || {
        echo "target build requires Ubuntu OpenCV development headers" >&2
        echo "Install libopencv-dev on this Ubuntu 20.04 target." >&2
        exit 1
    }
    [[ -f /usr/include/spdlog/spdlog.h ]] || {
        echo "target build requires spdlog development headers" >&2
        echo "Install libspdlog-dev on this Ubuntu 20.04 target." >&2
        exit 1
    }
    target_build_root="$(mktemp -d /tmp/taccap-target-build.XXXXXX)"
    mkdir -p "$target_build_root/source" "$target_build_root/wheel" "$target_build_root/wheels"
    tar -xzf "$offline_dir/taccap-source.tar.gz" -C "$target_build_root/source"
    tar -xzf "$offline_dir/build-wheels.tar.gz" -C "$target_build_root/wheels"
    fmt_prefix=""
    if [[ -f "$offline_dir/fmt-headers.tar.gz" ]]; then
        fmt_prefix="$target_build_root/fmt"
        mkdir -p "$fmt_prefix"
        tar -xzf "$offline_dir/fmt-headers.tar.gz" -C "$fmt_prefix"
        mkdir -p "$fmt_prefix/lib/cmake/fmt"
        cat >"$fmt_prefix/lib/cmake/fmt/fmtConfig.cmake" <<FMT
set(fmt_FOUND TRUE)
set(fmt_VERSION 8.1.1)
if(NOT TARGET fmt::fmt)
    add_library(fmt::fmt INTERFACE IMPORTED)
    set_target_properties(fmt::fmt PROPERTIES INTERFACE_INCLUDE_DIRECTORIES "$fmt_prefix/include")
endif()
if(NOT TARGET fmt::fmt-header-only)
    add_library(fmt::fmt-header-only INTERFACE IMPORTED)
    set_target_properties(fmt::fmt-header-only PROPERTIES INTERFACE_INCLUDE_DIRECTORIES "$fmt_prefix/include")
endif()
FMT
        cp "$fmt_prefix/lib/cmake/fmt/fmtConfig.cmake" \
            "$fmt_prefix/lib/cmake/fmt/fmt-config.cmake"
    fi
    "$preflight_python" - "$target_build_root/source/python/CMakeLists.txt" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
needle = "find_package(spdlog REQUIRED)"
if "find_package(fmt CONFIG REQUIRED)" not in text:
    if needle not in text:
        raise SystemExit(f"cannot patch TacCap CMake file: {path}")
    text = text.replace(
        needle,
        "find_package(fmt CONFIG REQUIRED)\n" + needle,
        1,
    )
    path.write_text(text)
PY
    target_build_venv="$target_build_root/venv"
    "$preflight_python" -m venv --clear "$target_build_venv"
    target_build_python="$target_build_venv/bin/python"
    [[ -x "$target_build_python" ]] || {
        echo "target Python could not create a build venv" >&2
        exit 1
    }
    export PATH="$target_build_venv/bin:$PATH"
    echo "Building TacCap-Gripper on target Ubuntu 20.04"
    "$target_build_python" -m pip install --disable-pip-version-check --no-index \
        --find-links "$target_build_root/wheels" \
        "setuptools>=68" "scikit-build-core>=0.10" "pybind11>=2.12" cmake ninja
    (
        unset CMAKE_PREFIX_PATH PKG_CONFIG_PATH TACCAP_CPP_PREFIX LD_LIBRARY_PATH
        if [[ -n "$fmt_prefix" ]]; then
            export CMAKE_PREFIX_PATH="$fmt_prefix"
        fi
        export PATH="$target_build_venv/bin:$PATH"
        "$target_build_python" -m pip wheel --disable-pip-version-check \
            --no-build-isolation --no-deps \
            "$target_build_root/source" -w "$target_build_root/wheel"
    )
    target_wheel="$(find "$target_build_root/wheel" -maxdepth 1 -type f \
        -name 'taccap_gripper-*.whl' -print -quit)"
    [[ -f "$target_wheel" ]] || {
        echo "TacCap wheel was not produced on target" >&2
        exit 1
    }
    "$target_build_python" -m pip install --disable-pip-version-check \
        --no-deps --target "$preflight_dir/site-packages" "$target_wheel"
fi
# A portable Python archive can be copied from a build host with a different
# locale.  Verify that its standard-library codecs are present before pip or
# the SDK import is attempted; this catches incomplete runtimes (for example,
# a missing encodings/cp437.py) at the bundle boundary.
if ! preflight_codec_output="$(
    env -u PYTHONPATH -u PYTHONHOME -u PYTHONUSERBASE \
    PYTHONNOUSERSITE=1 "$preflight_python" 2>&1 <<'PY'
import codecs
codecs.lookup("cp437")
print("verified codec: cp437")
PY
)"; then
    printf '%s\n' "$preflight_codec_output" >&2
    echo "ERROR: offline Python runtime is incomplete (cp437 codec is missing)." >&2
    echo "Regenerate the bundle with a complete Python 3.12 runtime." >&2
    exit 1
fi
printf '%s\n' "$preflight_codec_output"
preflight_output=""
# Do not inherit ROS/conda/old-SDK libraries from the target shell.  The
# target-built extension resolves against this machine's default Ubuntu 20.04
# loader paths, plus libraries explicitly shipped in this bundle.
if ! preflight_output="$(
    env -u PYTHONPATH -u PYTHONHOME -u PYTHONUSERBASE \
    PYTHONNOUSERSITE=1 \
    PYTHONPATH="$preflight_dir/site-packages" \
    LD_LIBRARY_PATH="$preflight_dir/lib" \
    "$preflight_python" - 2>&1 <<'PY'
import importlib

for module in ("xensesdk", "xense.taccap"):
    importlib.import_module(module)
    print(f"verified import: {module}")
PY
)"; then
    printf '%s\n' "$preflight_output" >&2
    echo >&2
    echo "ERROR: offline xense.taccap native extension is incompatible with this target." >&2
    if grep -qE 'cannot open shared object file|=> not found' <<<"$preflight_output"; then
        echo "ERROR: the bundle references a shared library that is not available on the target." >&2
        echo "ERROR: for Ubuntu 20.04, use a TacCap wheel built against its system OpenCV 4.2 libraries." >&2
    elif grep -qE 'GLIBC_[0-9].*not found|GLIBCXX_[0-9].*not found|CXXABI_[0-9].*not found' <<<"$preflight_output"; then
        echo "ERROR: the bundle was built against a newer glibc/libstdc++ ABI than this target." >&2
    fi
    echo "ERROR: deployment aborted before stopping the existing service." >&2
    echo "ERROR: no /home/guest/py312, system Python, or old SDK fallback is attempted." >&2
    echo "Build TacCap-Gripper on the target Ubuntu 20.04 system, or provide a compatible wheel bundle." >&2
    exit 1
fi
printf '%s\n' "$preflight_output"

# The bundle passed its ABI preflight.  Stop only the existing TacCap service
# in the requested installation directory before replacing its source/runtime.
# This also handles the old flat layout used by early releases.
if [[ -x "$install_dir/scripts/taccap.sh" ]]; then
    "$install_dir/scripts/taccap.sh" stop >/dev/null 2>&1 || true
elif [[ -x "$install_dir/taccap.sh" ]]; then
    "$install_dir/taccap.sh" stop >/dev/null 2>&1 || true
fi

mkdir -p "$install_dir/.runtime"
runtime_stage="$(mktemp -d "$install_dir/.runtime/.python.XXXXXX")"
runtime_cleanup() { rm -rf -- "$runtime_stage"; }
trap runtime_cleanup EXIT
tar -cf - -C "$offline_dir/python" . | tar -xf - -C "$runtime_stage"
python_home="$install_dir/.runtime/python"
if [[ -e "$python_home" ]]; then
    [[ -f "$install_dir/.runtime/.managed-by-taccap" ]] || {
        echo "refusing to replace unmanaged runtime: $python_home" >&2; exit 1;
    }
    rm -rf -- "$python_home"
fi
mv -- "$runtime_stage" "$python_home"
runtime_stage=""
trap - EXIT
: >"$install_dir/.runtime/.managed-by-taccap"
base_python="$(find "$python_home" \( -type f -o -type l \) -path '*/bin/python3.12' -print -quit)"
[[ -x "$base_python" ]] || { echo "offline bundle has no Python 3.12" >&2; exit 1; }

runtime_lib_dir="$install_dir/.runtime/lib"
if [[ -f "$offline_dir/runtime-libs.tar.gz" ]]; then
    runtime_lib_stage="$(mktemp -d "$install_dir/.runtime/.lib.XXXXXX")"
    tar -xzf "$offline_dir/runtime-libs.tar.gz" -C "$runtime_lib_stage"
    if [[ -e "$runtime_lib_dir" ]]; then
        [[ -f "$install_dir/.runtime/.managed-by-taccap" ]] || {
            echo "refusing to replace unmanaged runtime library directory: $runtime_lib_dir" >&2
            rm -rf -- "$runtime_lib_stage"
            exit 1
        }
        rm -rf -- "$runtime_lib_dir"
    fi
    mv -- "$runtime_lib_stage" "$runtime_lib_dir"
else
    if [[ -e "$runtime_lib_dir" ]]; then
        [[ -f "$install_dir/.runtime/.managed-by-taccap" ]] || {
            echo "refusing to remove unmanaged runtime library directory: $runtime_lib_dir" >&2
            exit 1
        }
        rm -rf -- "$runtime_lib_dir"
    fi
    runtime_lib_dir=""
fi

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
# Migrate the complete legacy tuning profile only when all of its values still
# match the old template. Older releases defaulted to kp=8/kd=1 plus a 0.60
# rad/s velocity feed-forward; that combination is under-damped on the TacCap
# mechanism and is a common reason a jaw keeps ringing after teleoperation.
# If any one value was deliberately changed, leave the whole custom profile
# untouched.
migrate_default_value() {
    local name="$1" old_value="$2" new_value="$3" tmp
    tmp="$(mktemp)"
    awk -v name="$name" -v old_value="$old_value" -v new_value="$new_value" '
        $0 ~ ("^" name "=") {
            value = substr($0, length(name) + 2)
            if (value == old_value) print name "=" new_value
            else print
            next
        }
        { print }
    ' "$install_dir/config/taccap.env" >"$tmp"
    mv -- "$tmp" "$install_dir/config/taccap.env"
}
legacy_tuning_profile=1
for legacy_pair in \
    'TACCAP_POSITION_KP=8.0' \
    'TACCAP_POSITION_KD=1.0' \
    'TACCAP_MIT_KP=8.0' \
    'TACCAP_MIT_KD=1.0' \
    'TACCAP_TARGET_MAX_VELOCITY_RAD_S=0.60'; do
    if ! grep -qxF "$legacy_pair" "$install_dir/config/taccap.env"; then
        legacy_tuning_profile=0
        break
    fi
done
if ((legacy_tuning_profile)); then
    migrate_default_value TACCAP_POSITION_KP 8.0 4.0
    migrate_default_value TACCAP_POSITION_KD 1.0 2.0
    migrate_default_value TACCAP_MIT_KP 8.0 4.0
    migrate_default_value TACCAP_MIT_KD 1.0 2.0
    migrate_default_value TACCAP_TARGET_MAX_VELOCITY_RAD_S 0.60 0.0
fi
# Recreate the managed venv contents on every deployment.  This prevents a
# removed dependency from surviving an upgrade as a stale package.
"$base_python" -m venv --clear "$install_dir/.venv"
python_bin="$install_dir/.venv/bin/python"
site_packages="$($python_bin -c 'import site; print(site.getsitepackages()[0])')"
[[ -d "$site_packages" ]] || { echo "target venv site-packages not found: $site_packages" >&2; exit 1; }
tar -xzf "$offline_dir/site-packages.tar.gz" -C "$site_packages"
if ((target_build)); then
    "$target_build_python" -m pip install --disable-pip-version-check \
        --no-deps --target "$site_packages" "$target_wheel"
fi
if [[ -n "$runtime_lib_dir" ]]; then
    export LD_LIBRARY_PATH="$runtime_lib_dir"
else
    # Match the preflight environment exactly.  In particular, do not let a
    # ROS/conda LD_LIBRARY_PATH make post-install verification import an old
    # SDK successfully by accident.
    unset LD_LIBRARY_PATH
fi

# Native extensions are part of the release contract.  A bundle built on a
# newer distribution must fail here when it requires newer glibc/libstdc++;
# never fall back to an interpreter or SDK left in /home/guest/py312 (or any
# other pre-existing environment).  Falling back would make a deployment look
# successful while silently changing the tactile SDK and control behavior.
verify_output=""
if ! verify_output="$(
    env -u PYTHONPATH -u PYTHONHOME -u PYTHONUSERBASE \
    PYTHONNOUSERSITE=1 \
    LD_LIBRARY_PATH="${runtime_lib_dir:-}" \
    "$python_bin" 2>&1 <<'PY'
import importlib

for module in ("xensesdk", "xense.taccap"):
    importlib.import_module(module)
    print(f"verified import: {module}")
PY
)"; then
    printf '%s\n' "$verify_output" >&2
    echo >&2
    echo "ERROR: offline xense.taccap native extension is incompatible with this target." >&2
    if grep -qE 'cannot open shared object file|=> not found' <<<"$verify_output"; then
        echo "ERROR: the bundle references a shared library that is not available on the target." >&2
        echo "ERROR: for Ubuntu 20.04, use a TacCap wheel built against its system OpenCV 4.2 libraries." >&2
    elif grep -qE 'GLIBC_[0-9].*not found|GLIBCXX_[0-9].*not found|CXXABI_[0-9].*not found' <<<"$verify_output"; then
        echo "ERROR: the bundle was built against a newer glibc/libstdc++ ABI than this target." >&2
    fi
    echo "ERROR: deployment aborted; no legacy Python/SDK fallback is attempted." >&2
    echo "Build the TacCap native extension on the target's OS/ABI (Ubuntu 20.04/glibc 2.31 for this device), then regenerate the offline bundle." >&2
    exit 1
fi
printf '%s\n' "$verify_output"
preflight_cleanup
trap - EXIT

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
set_config_value TACCAP_LD_LIBRARY_PATH "${runtime_lib_dir:-}"
# Do not inherit a native module from an older installation.  A successful
# deployment must use only the verified module inside the new bundle.
set_config_value TACCAP_PYTHONPATH ""

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
