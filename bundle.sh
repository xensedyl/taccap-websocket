#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: ./bundle.sh [options]

Create a complete offline release bundle on the connected development host.
The target receives a portable Python runtime and an installed site-packages
snapshot. It does not need Python, pip, uv, Git, a compiler or network access.

The vendor SDKs are installed from their normal sources:
  xensesdk       installed by package name (pip/PyPI or configured index)
  taccap-gripper installed from the TacCap-Gripper source repository

Options:
  --output DIR                 Output directory (default: ./offline)
  --xensesdk SPEC              xensesdk requirement (default: xensesdk==2.1.3)
  --taccap-source PATH|URL     Source checkout or Git URL
                               (default: XenseRobotics-AI/TacCap-Gripper)
  --python PATH                Resolver Python 3.12+ (default: python3)
  -h, --help                   Show this help

The build host must have network access for xensesdk and the source-build
dependencies. The C++ build also needs OpenCV development libraries and
spdlog (from the host or from TACCAP_CPP_PREFIX). No wheel is copied into the
release bundle; pip may create temporary artifacts while installing the
source package.
USAGE
}

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_dir="$project_dir/offline"
python_bin="${TACCAP_BUNDLE_PYTHON:-python3}"
xensesdk_requirement="${TACCAP_XENSESDK_REQUIREMENT:-xensesdk==2.1.3}"
taccap_source="${TACCAP_TACCAP_SOURCE:-https://github.com/XenseRobotics-AI/TacCap-Gripper.git}"

while (($#)); do
    case "$1" in
        --output)
            (($# >= 2)) || { echo "missing argument for --output" >&2; exit 2; }
            output_dir="$2"; shift 2 ;;
        --xensesdk)
            (($# >= 2)) || { echo "missing argument for --xensesdk" >&2; exit 2; }
            xensesdk_requirement="$2"; shift 2 ;;
        --taccap-source)
            (($# >= 2)) || { echo "missing argument for --taccap-source" >&2; exit 2; }
            taccap_source="$2"; shift 2 ;;
        --python)
            (($# >= 2)) || { echo "missing argument for --python" >&2; exit 2; }
            python_bin="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v "$python_bin" >/dev/null 2>&1 || [[ -x "$python_bin" ]] || {
    echo "resolver Python not found: $python_bin" >&2
    exit 1
}
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
    echo "bundle resolver Python 3.12 or newer is required" >&2
    exit 1
}
command -v uv >/dev/null 2>&1 || {
    echo "uv is required on the connected build host" >&2
    exit 1
}
command -v git >/dev/null 2>&1 || {
    echo "git is required to identify/install TacCap-Gripper" >&2
    exit 1
}
command -v c++ >/dev/null 2>&1 || {
    echo "a C++ compiler (c++) is required to build TacCap-Gripper from source" >&2
    exit 1
}

# TacCap-Gripper's CMake project links the C++ OpenCV and spdlog packages.
# A conda/mamba prefix is common on the development host; make it explicit so
# CMake does not accidentally pick a different ABI from an unrelated prefix.
if [[ -n "${TACCAP_CPP_PREFIX:-}" ]]; then
    [[ -d "$TACCAP_CPP_PREFIX" ]] || {
        echo "TACCAP_CPP_PREFIX is not a directory: $TACCAP_CPP_PREFIX" >&2
        exit 1
    }
    if [[ -d "$TACCAP_CPP_PREFIX/bin" ]]; then
        export PATH="$TACCAP_CPP_PREFIX/bin:$PATH"
    fi
    export CMAKE_PREFIX_PATH="$TACCAP_CPP_PREFIX${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
    if [[ -d "$TACCAP_CPP_PREFIX/lib/pkgconfig" ]]; then
        export PKG_CONFIG_PATH="$TACCAP_CPP_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    fi
fi

case "$output_dir" in
    /|/home|/root|/tmp)
        echo "refusing unsafe output directory: $output_dir" >&2
        exit 2
        ;;
esac

source_dir=""
source_tmp=""
build_root=""
runtime_tmp=""
cleanup() {
    [[ -n "$build_root" ]] && rm -rf -- "$build_root"
    [[ -n "$source_tmp" ]] && rm -rf -- "$source_tmp"
    [[ -n "$runtime_tmp" ]] && rm -rf -- "$runtime_tmp"
}
trap cleanup EXIT

if [[ -d "$taccap_source" ]]; then
    source_dir="$(cd -- "$taccap_source" && pwd)"
elif [[ "$taccap_source" =~ ^(https?|ssh|git)://|^git@ ]]; then
    source_tmp="$(mktemp -d /tmp/taccap-gripper-source.XXXXXX)"
    echo "Cloning TacCap-Gripper source: $taccap_source"
    git clone --depth 1 "$taccap_source" "$source_tmp/TacCap-Gripper"
    source_dir="$source_tmp/TacCap-Gripper"
else
    echo "TacCap-Gripper source directory or Git URL not found: $taccap_source" >&2
    echo "Use --taccap-source /path/to/TacCap-Gripper or the repository URL." >&2
    exit 1
fi
[[ -f "$source_dir/pyproject.toml" ]] || {
    echo "TacCap-Gripper source has no pyproject.toml: $source_dir" >&2
    exit 1
}

rm -rf -- "$output_dir"
mkdir -p "$output_dir"

echo "[1/6] Copying portable Python 3.12"
if [[ -n "${TACCAP_BUNDLE_RUNTIME:-}" ]]; then
    [[ -d "$TACCAP_BUNDLE_RUNTIME" ]] || {
        echo "runtime directory not found: $TACCAP_BUNDLE_RUNTIME" >&2
        exit 1
    }
    mkdir -p "$output_dir/python"
    tar -cf - -C "$TACCAP_BUNDLE_RUNTIME" . | tar -xf - -C "$output_dir/python"
else
    runtime_tmp="$(mktemp -d /tmp/taccap-python-runtime.XXXXXX)"
    uv python install 3.12 --install-dir "$runtime_tmp" --no-bin
    mkdir -p "$output_dir/python"
    tar -cf - -C "$runtime_tmp" . | tar -xf - -C "$output_dir/python"
fi
# uv creates a convenient top-level alias using an absolute link into its
# install directory.  Rewrite such links relative to the copied runtime so
# they remain valid after the temporary build directory is removed.
while IFS= read -r -d '' runtime_link; do
    link_target="$(readlink "$runtime_link")"
    if [[ "$link_target" = /* ]]; then
        target_name="$(basename "$link_target")"
        [[ -d "$output_dir/python/$target_name" ]] || {
            echo "portable Python alias has no copied target: $runtime_link -> $link_target" >&2
            exit 1
        }
        ln -sfn "$target_name" "$runtime_link"
    fi
done < <(find "$output_dir/python" -maxdepth 1 -type l -print0)
bundle_python="$(find "$output_dir/python" \( -type f -o -type l \) \
    -path '*/bin/python3.12' -print -quit)"
[[ -x "$bundle_python" ]] || {
    echo "portable Python executable not found" >&2
    exit 1
}

build_root="$(mktemp -d /tmp/taccap-runtime-build.XXXXXX)"
build_venv="$build_root/venv"
echo "[2/6] Creating temporary build environment"
uv venv --seed --python "$bundle_python" "$build_venv"
build_python="$build_venv/bin/python"
[[ -x "$build_python" ]] || {
    echo "temporary build Python was not created" >&2
    exit 1
}
# pip-installed cmake/ninja are used by the source build.  Keep them ahead of
# any system or conda tools while installing TacCap-Gripper.
export PATH="$build_venv/bin:$PATH"

echo "[3/6] Installing source-build tools"
"$build_python" -m pip install --disable-pip-version-check --no-cache-dir \
    "setuptools>=68" "scikit-build-core>=0.10" "pybind11>=2.12" cmake ninja

echo "[4/6] Installing public dependencies and xensesdk"
"$build_python" -m pip install --disable-pip-version-check --no-cache-dir \
    numpy==2.2.4 opencv-python==4.12.0.88 "$xensesdk_requirement"

echo "[5/6] Building/installing TacCap-Gripper from source"
"$build_python" -m pip install --disable-pip-version-check --no-cache-dir \
    --no-build-isolation "$source_dir"

# Install this project into the same environment so its package metadata and
# console entry point are available when the snapshot is restored on target.
"$build_python" -m pip install --disable-pip-version-check --no-cache-dir \
    --no-build-isolation --no-deps "$project_dir"

site_packages="$("$build_python" -c 'import site; print(site.getsitepackages()[0])')"
[[ -d "$site_packages" ]] || {
    echo "temporary site-packages directory not found: $site_packages" >&2
    exit 1
}
# A native TacCap build may use libraries from TACCAP_CPP_PREFIX.  Put that
# prefix first while checking imports and collecting the small transitive
# runtime closure below; the target itself may not have the same ABI.
if [[ -n "${TACCAP_CPP_PREFIX:-}" ]]; then
    export LD_LIBRARY_PATH="$TACCAP_CPP_PREFIX/lib:$site_packages/xense/taccap${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

runtime_libs_archive="$output_dir/runtime-libs.tar.gz"
if [[ -n "${TACCAP_CPP_PREFIX:-}" ]]; then
    runtime_libs_stage="$(mktemp -d /tmp/taccap-runtime-libs.XXXXXX)"
    "$build_python" - "$site_packages" "$TACCAP_CPP_PREFIX" "$runtime_libs_stage" <<'PY'
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

site_packages = Path(sys.argv[1]).resolve()
prefix = Path(sys.argv[2]).resolve()
stage = Path(sys.argv[3]).resolve()
prefix_lib = (prefix / "lib").resolve()
stage.mkdir(parents=True, exist_ok=True)

needed = {}
queue = [p for p in site_packages.rglob("*.so*") if p.is_file()]
seen = set()
patterns = (
    re.compile(r"=>\s+(?P<path>/[^\s]+)\s+\("),
    re.compile(r"^\s*(?P<path>/[^\s]+)\s+\("),
)

while queue:
    item = queue.pop()
    try:
        item = item.resolve()
    except FileNotFoundError:
        continue
    if item in seen:
        continue
    seen.add(item)
    proc = subprocess.run(
        ["ldd", str(item)], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    for line in proc.stdout.splitlines():
        match = next((p.search(line) for p in patterns if p.search(line)), None)
        if not match:
            continue
        dep = Path(match.group("path"))
        if not dep.is_absolute() or not dep.exists():
            continue
        dep_real = dep.resolve()
        # Native dependencies can intentionally mix a prefix library with a
        # compatible system library (e.g. Ubuntu's libcblas.so.3).  Capture
        # prefix files, while also recording any unresolved SONAME that is
        # available on the connected host so the target gets a self-contained
        # closure even when its distro is older/minimal.
        if str(dep_real).startswith(str(prefix_lib) + os.sep) or dep.name in {
            "libatlas.so.3", "libcblas.so.3", "libblas.so.3", "liblapack.so.3",
        }:
            # Keep every SONAME alias and the final regular file.  Copying
            # only a symlink would leave a dangling link in the target bundle.
            current = dep
            while True:
                needed[current.name] = current
                if not current.is_symlink():
                    break
                current = (current.parent / os.readlink(current)).resolve()
            queue.append(dep_real)

for name, source in sorted(needed.items()):
    destination = stage / name
    if destination.exists() or destination.is_symlink():
        continue
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)

if not needed:
    raise SystemExit(f"no runtime libraries from {prefix_lib} were discovered")
print(f"collected {len(needed)} runtime libraries into {stage}")
PY
    tar -czf "$runtime_libs_archive" -C "$runtime_libs_stage" .
    rm -rf -- "$runtime_libs_stage"
else
    runtime_libs_archive=""
fi

"$build_python" - <<'PY'
import importlib

for module in ("xensesdk", "xense.taccap"):
    importlib.import_module(module)
    print(f"verified import: {module}")
PY

echo "[6/6] Packing installed Python dependencies"
tar -czf "$output_dir/site-packages.tar.gz" -C "$site_packages" .

source_commit="$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || echo unknown)"
source_remote="$(git -C "$source_dir" remote get-url origin 2>/dev/null || printf '%s' "$taccap_source")"
manifest="$output_dir/manifest.txt"
{
    printf 'bundle_created=%s\n' "$(date --iso-8601=seconds)"
    printf 'project_commit=%s\n' "$(git -C "$project_dir" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'xensesdk_requirement=%s\n' "$xensesdk_requirement"
    printf 'taccap_source=%s\n' "$source_remote"
    printf 'taccap_commit=%s\n\n' "$source_commit"
    printf 'sha256:\n'
    (cd "$output_dir" && sha256sum site-packages.tar.gz)
    if [[ -n "$runtime_libs_archive" ]]; then
        (cd "$output_dir" && sha256sum "$(basename "$runtime_libs_archive")")
    fi
} >"$manifest"

echo "Offline release ready: $output_dir"
du -sh "$output_dir"
echo "Next: ./deploy.sh user@TARGET --bundle $output_dir"
