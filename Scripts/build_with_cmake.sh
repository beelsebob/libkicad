#!/bin/sh
# Xcode's libkicad target delegates here.  The archive is written to the same
# BUILT_PRODUCTS_DIR Xcode exposes to projects which depend on libkicad.
set -eu

configuration="${1:?missing Xcode configuration}"
output_directory="${2:?missing Xcode built-products directory}"

case "$configuration" in
    Debug) build_type=Debug ;;
    Release) build_type=Release ;;
    *) echo "error: unsupported Xcode configuration: $configuration" >&2; exit 2 ;;
esac

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_directory=$(CDPATH= cd -- "$script_directory/.." && pwd)
build_directory="$source_directory/build/cmake/xcode-$configuration"

cmake -S "$source_directory" -B "$build_directory" -G Ninja \
    -DCMAKE_BUILD_TYPE="$build_type" \
    -DCMAKE_ARCHIVE_OUTPUT_DIRECTORY="$output_directory" \
    -DCMAKE_OSX_ARCHITECTURES="${ARCHS:-arm64}" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-}"
cmake --build "$build_directory" --target libkicad
