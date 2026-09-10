#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONNOUSERSITE=1

apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build pkg-config \
    libopencv-core-dev libopencv-videoio-dev \
    libopencv-calib3d-dev libopencv-imgproc-dev \
    libspdlog-dev ca-certificates
rm -rf /var/lib/apt/lists/*

# The runtime is mounted read-only so the caller's bundle cannot be modified
# by pip. Work on a writable copy inside the short-lived build container.
rm -rf /tmp/taccap-runtime
cp -a /opt/taccap-runtime /tmp/taccap-runtime
python_bin="$(find /tmp/taccap-runtime -type f -path '*/bin/python3.12' -print -quit)"
[[ -x "$python_bin" ]] || { echo "portable Python 3.12 was not found" >&2; exit 1; }

# scikit-build-core writes its build tree below the source checkout. Keep the
# caller's checkout read-only and build from a writable temporary copy.
rm -rf /tmp/TacCap-Gripper
cp -a /opt/taccap-source /tmp/TacCap-Gripper

"$python_bin" -m pip install --no-cache-dir \
    --break-system-packages \
    'setuptools>=68' 'scikit-build-core>=0.10' 'pybind11>=2.12' cmake ninja
export PATH="$(dirname "$python_bin"):$PATH"

opencv_config_dir="$(dirname "$(find /usr -type f -name OpenCVConfig.cmake -print -quit)")"
[[ -f "$opencv_config_dir/OpenCVConfig.cmake" ]] || {
    echo "Ubuntu 20.04 OpenCVConfig.cmake was not found" >&2
    exit 1
}
export CMAKE_ARGS="-DOpenCV_DIR=$opencv_config_dir"
"$python_bin" -m pip wheel \
    --no-build-isolation --no-deps \
    --config-settings="cmake.define.OpenCV_DIR=$opencv_config_dir" \
    /tmp/TacCap-Gripper -w /opt/taccap-output

wheel="$(find /opt/taccap-output -maxdepth 1 -type f -name 'taccap_gripper-*.whl' -print -quit)"
[[ -n "$wheel" ]] || { echo "TacCap wheel was not produced" >&2; exit 1; }
echo "Built $(basename "$wheel")"
