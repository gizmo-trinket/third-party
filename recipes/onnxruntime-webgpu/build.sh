#!/usr/bin/env bash
# Builds ONNX Runtime from a checkout at SRC_DIR into INSTALL_DIR.
#
#   SRC_DIR     the upstream checkout, at the recipe's commit, submodules included
#   INSTALL_DIR where `cmake --install` puts the result; the recipe's `keep` picks from it
#   FLAGS_FILE  the platform's flags from the recipe, NUL-separated
#
# The flags end with --cmake_extra_defines' values, so CMAKE_INSTALL_PREFIX is appended
# to that list.
#
# macOS runners ship bash 3.2 as /bin/bash: no mapfile, so the flags are read with a
# loop, and nothing newer than 3.2 is used.
set -euo pipefail

: "${SRC_DIR:?}" "${INSTALL_DIR:?}" "${FLAGS_FILE:?}"
flags=()
while IFS= read -r -d '' f; do flags+=("$f"); done < "$FLAGS_FILE"
(( ${#flags[@]} > 0 )) || { echo "no flags in $FLAGS_FILE" >&2; exit 1; }

command -v ninja >/dev/null || brew install ninja

build_dir="$SRC_DIR/build"
cd "$SRC_DIR"
bash build.sh \
  --build_dir "$build_dir" \
  --config Release \
  --parallel \
  --cmake_generator Ninja \
  "${flags[@]}" \
  "CMAKE_INSTALL_PREFIX=$INSTALL_DIR"

cmake --install "$build_dir/Release" --config Release
