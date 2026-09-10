#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: build_taccap_wheel_container.sh RUNTIME_DIR SOURCE_DIR OUTPUT_DIR [DOCKER]

Build TacCap-Gripper from its Git checkout in an Ubuntu 20.04 container.
The output directory receives a temporary cp312 wheel.  The wheel is an
intermediate artifact; bundle.sh unpacks it into site-packages and does not
put the wheel in the release or repository.
USAGE
}

(($# >= 3 && $# <= 4)) || { usage >&2; exit 2; }
runtime_dir="$(cd -- "$1" && pwd)"
source_dir="$(cd -- "$2" && pwd)"
output_dir="$(cd -- "$3" && pwd)"
docker_cmd="${4:-docker}"
image="${TACCAP_BUILDER_IMAGE:-ubuntu:20.04}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
inner_script="$script_dir/build_taccap_wheel_container_inner.sh"

[[ -d "$runtime_dir" ]] || { echo "runtime directory not found: $runtime_dir" >&2; exit 1; }
[[ -f "$source_dir/pyproject.toml" ]] || { echo "TacCap source has no pyproject.toml: $source_dir" >&2; exit 1; }
[[ -f "$inner_script" ]] || { echo "container build script not found: $inner_script" >&2; exit 1; }
mkdir -p "$output_dir"

echo "Building TacCap-Gripper wheel in $image (Ubuntu 20.04 ABI)"
"$docker_cmd" run --rm \
    -e DEBIAN_FRONTEND=noninteractive \
    -v "$runtime_dir:/opt/taccap-runtime:ro" \
    -v "$source_dir:/opt/taccap-source:ro" \
    -v "$output_dir:/opt/taccap-output" \
    -v "$inner_script:/opt/build-taccap-wheel.sh:ro" \
    "$image" bash /opt/build-taccap-wheel.sh

wheel_path="$(find "$output_dir" -maxdepth 1 -type f -name 'taccap_gripper-*.whl' -print -quit)"
[[ -n "$wheel_path" ]] || { echo "TacCap wheel was not produced by container" >&2; exit 1; }
printf '%s\n' "$wheel_path"
