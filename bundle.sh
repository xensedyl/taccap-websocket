#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: ./bundle.sh [options]

Create a complete offline release bundle on the connected development host.
The target device receives this bundle through deploy.sh and needs no Python,
pip, uv, Git, compiler or network access.

Options:
  --output DIR             Output directory (default: ./offline)
  --xensesdk-wheel PATH    xensesdk wheel (required; kept outside Git)
  --taccap-wheel PATH      taccap-gripper wheel (required; kept outside Git)
  --python PATH            Resolver Python 3.12+ (default: python3)
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
        --output) (($# >= 2)) || { echo "missing argument for --output" >&2; exit 2; }; output_dir="$2"; shift 2 ;;
        --xensesdk-wheel) (($# >= 2)) || { echo "missing argument for --xensesdk-wheel" >&2; exit 2; }; xensesdk_wheel="$2"; shift 2 ;;
        --taccap-wheel) (($# >= 2)) || { echo "missing argument for --taccap-wheel" >&2; exit 2; }; taccap_wheel="$2"; shift 2 ;;
        --python) (($# >= 2)) || { echo "missing argument for --python" >&2; exit 2; }; python_bin="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ -n "$xensesdk_wheel" && -f "$xensesdk_wheel" ]] || {
    echo "xensesdk wheel is required and must be outside Git; pass --xensesdk-wheel PATH" >&2
    exit 1
}
[[ -n "$taccap_wheel" && -f "$taccap_wheel" ]] || {
    echo "taccap-gripper wheel is required and must be outside Git; pass --taccap-wheel PATH" >&2
    exit 1
}
command -v "$python_bin" >/dev/null 2>&1 || [[ -x "$python_bin" ]] || {
    echo "resolver Python not found: $python_bin" >&2; exit 1;
}
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
    echo "bundle resolver Python 3.12 or newer is required" >&2; exit 1;
}
command -v uv >/dev/null 2>&1 || { echo "uv is required on the connected build host" >&2; exit 1; }

case "$output_dir" in /|/home|/root|/tmp) echo "refusing unsafe output directory: $output_dir" >&2; exit 2;; esac
rm -rf -- "$output_dir"
mkdir -p "$output_dir/wheels"

echo "[1/4] Copying portable Python 3.12"
uv python install 3.12 --install-dir "$output_dir/python" --no-bin
bundle_python="$(find "$output_dir/python" \( -type f -o -type l \) \
    -path '*/bin/python3.12' -print -quit)"
[[ -x "$bundle_python" ]] || { echo "portable Python executable not found" >&2; exit 1; }

echo "[2/4] Copying private SDK wheels"
cp -p "$xensesdk_wheel" "$output_dir/wheels/"
cp -p "$taccap_wheel" "$output_dir/wheels/"
cypack_wheel="$(find /home/xense/.cache/pip/wheels /home/xense/.cache/uv \
    -type f -name 'cypack-0.1.2-*.whl' -print -quit 2>/dev/null || true)"
[[ -f "$cypack_wheel" ]] || {
    echo "cypack 0.1.2 wheel not found in local cache" >&2
    echo "Build/install it on the connected host, then retry." >&2
    exit 1
}
cp -p "$cypack_wheel" "$output_dir/wheels/"

echo "[3/4] Resolving all Python dependencies"
"$bundle_python" -m pip download --only-binary=:all: \
    --dest "$output_dir/wheels" --find-links "$output_dir/wheels" \
    --python-version 3.12 -r "$project_dir/requirements.txt"

echo "[4/4] Writing release manifest"
manifest="$output_dir/manifest.txt"
{
    printf 'bundle_created=%s\n' "$(date --iso-8601=seconds)"
    printf 'project_commit=%s\n' "$(git -C "$project_dir" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'xensesdk_wheel=%s\n' "$(basename "$xensesdk_wheel")"
    printf 'taccap_wheel=%s\n' "$(basename "$taccap_wheel")"
    printf 'taccap_source=https://github.com/XenseRobotics-AI/TacCap-Gripper.git\n'
    printf 'wheel_count=%s\n\n' "$(find "$output_dir/wheels" -maxdepth 1 -type f -name '*.whl' | wc -l)"
    printf 'sha256:\n'
    (cd "$output_dir" && sha256sum wheels/*.whl)
} >"$manifest"

echo "Offline release ready: $output_dir"
du -sh "$output_dir"
echo "Next: ./deploy.sh user@TARGET --bundle $output_dir"
