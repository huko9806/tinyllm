#!/usr/bin/env bash
# Builds llama-server from source, puts it in llama/ next to this script and cleans up afterwards.
# Needs the Python environment .venv/ next to this script: cmake and ninja are installed there
# for the build and removed again.
set -euo pipefail

VERSION=${1:-v0.9.0}
AI=$(dirname "$(readlink -f "$0")")
# Not /tmp: on the Pi it lives in RAM and is too small for the build.
WORK=$HOME/.cache/tinyllm-build

if [ ! -x "$AI/.venv/bin/pip" ]; then
  echo "No Python environment yet. Create it first: python3 -m venv $AI/.venv" >&2
  exit 1
fi
export TMPDIR=$WORK/tmp
export PATH=$AI/.venv/bin:$PATH

rm -rf "$WORK"
mkdir -p "$TMPDIR" "$AI/llama"

pip install --quiet --disable-pip-version-check cmake ninja
git clone --quiet --depth 1 --branch "$VERSION" https://github.com/ggml-org/llama.cpp.git "$WORK/src"

cmake -S "$WORK/src" -B "$WORK/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=ON \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_TOOLS=ON \
  -DLLAMA_BUILD_SERVER=ON

# Only 2 jobs: some server source files need a lot of RAM to compile.
cmake --build "$WORK/build" --target llama-server -j 2

cp "$WORK/build/bin/llama-server" "$AI/llama/llama-server"
strip "$AI/llama/llama-server"
rm -rf "$WORK"
pip uninstall --quiet --yes --disable-pip-version-check cmake ninja
"$AI/llama/llama-server" --version
