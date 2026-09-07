#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bundle_offline.sh [options]

Create an offline deployment bundle on a networked development machine.
The target device receives the bundle with deploy.sh and does not need
Python, uv, pip, or internet access.

Options:
  --xensesdk-wheel PATH   xensesdk wheel (required)
  --taccap-wheel PATH     taccap-gripper wheel (default: vendor/wheels/*.whl)
  --output DIR             Bundle directory (default: ./offline)
  --python PATH            Python used to resolve wheels (default: python3)
  -h, --help               Show this help
USAGE
}

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_dir="$project_dir/offline"
python_bin="${TACCAP_BUNDLE_PYTHON:-python3}"
xensesdk_wheel=""
taccap_wheel=""

while (($#)); do
    case "$1" in
        --xensesdk-wheel)
            (($# >= 2)) || { echo "missing argument for --xensesdk-wheel" >&2; exit 2; }
            xensesdk_wheel="$2"
            shift 2
            ;;
        --taccap-wheel)
            (($# >= 2)) || { echo "missing argument for --taccap-wheel" >&2; exit 2; }
            taccap_wheel="$2"
            shift 2
            ;;
        --output)
            (($# >= 2)) || { echo "missing argument for --output" >&2; exit 2; }
            output_dir="$2"
            shift 2
            ;;
        --python)
            (($# >= 2)) || { echo "missing argument for --python" >&2; exit 2; }
            python_bin="$2"
            shift 2
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

if [[ -z "$xensesdk_wheel" ]]; then
    xensesdk_wheel="$(find "$project_dir" /home/xense/rebot_lerobot /home/xense/xensehand-lerobot \
        -maxdepth 5 -type f -name 'xensesdk-*-cp312-*-linux_x86_64.whl' \
        -print 2>/dev/null | sort -V | tail -n 1)"
fi
if [[ -z "$taccap_wheel" ]]; then
    taccap_wheel="$(find "$project_dir/vendor/wheels" -maxdepth 1 -type f \
        -name 'taccap_gripper-*.whl' -print -quit 2>/dev/null || true)"
fi
[[ -f "$xensesdk_wheel" ]] || {
    echo "xensesdk wheel not found; pass --xensesdk-wheel PATH" >&2
    exit 1
}
[[ -f "$taccap_wheel" ]] || {
    echo "taccap-gripper wheel not found; pass --taccap-wheel PATH" >&2
    exit 1
}
command -v "$python_bin" >/dev/null 2>&1 || [[ -x "$python_bin" ]] || {
    echo "Python executable not found: $python_bin" >&2
    exit 1
}
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
    echo "bundle resolver Python must be 3.12 or newer" >&2
    exit 1
}
command -v uv >/dev/null 2>&1 || {
    echo "uv is required to package the portable Python runtime" >&2
    exit 1
}

rm -rf -- "$output_dir"
mkdir -p "$output_dir/wheels"

# This interpreter is copied verbatim to the target. uv downloads it only on
# the connected build machine; install.sh never invokes uv on an offline host.
uv python install 3.12 --install-dir "$output_dir/python" --no-bin
bundle_python="$(find "$output_dir/python" \( -type f -o -type l \) \
    -path '*/bin/python3.12' -print -quit)"
[[ -x "$bundle_python" ]] || {
    echo "uv Python executable not found under $output_dir/python" >&2
    exit 1
}

cp -p "$xensesdk_wheel" "$output_dir/wheels/"
cp -p "$taccap_wheel" "$output_dir/wheels/"

# xensesdk 2.1.3 currently asks for cypack>=0.1.2, while the public index may
# only expose 0.1.1. Keep the vendor wheel local so resolution is repeatable.
cypack_wheel="$(find /home/xense/.cache/pip/wheels /home/xense/.cache/uv \
    -type f -name 'cypack-0.1.2-*.whl' -print -quit 2>/dev/null || true)"
[[ -f "$cypack_wheel" ]] || {
    echo "cypack 0.1.2 wheel not found in local cache" >&2
    echo "Install/build cypack 0.1.2 on the connected machine, then retry." >&2
    exit 1
}
cp -p "$cypack_wheel" "$output_dir/wheels/"

# Resolve the complete transitive closure in one pass. Public dependencies may
# be fetched here on the connected build machine; the resulting directory is
# installed with --no-index on the target.
"$bundle_python" -m pip download \
    --only-binary=:all: \
    --dest "$output_dir/wheels" \
    --find-links "$output_dir/wheels" \
    --python-version 3.12 \
    -r "$project_dir/requirements.txt"

manifest="$output_dir/manifest.txt"
{
    printf 'bundle_created=%s\n' "$(date --iso-8601=seconds)"
    printf 'project_commit=%s\n' "$(git -C "$project_dir" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'xensesdk_wheel=%s\n' "$(basename "$xensesdk_wheel")"
    printf 'taccap_wheel=%s\n' "$(basename "$taccap_wheel")"
    printf 'taccap_source=https://github.com/XenseRobotics-AI/TacCap-Gripper.git\n'
    printf 'wheel_count=%s\n' "$(find "$output_dir/wheels" -maxdepth 1 -type f -name '*.whl' | wc -l)"
    printf '\nsha256:\n'
    (cd "$output_dir" && sha256sum wheels/*.whl)
} >"$manifest"

echo "Offline bundle created: $output_dir"
du -sh "$output_dir"
echo "Deploy with: ./deploy.sh user@TARGET --offline-dir $output_dir --enable-systemd"
