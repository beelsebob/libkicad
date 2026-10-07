#!/bin/bash
# Configures and builds the parts of the KiCad submodule (submodules/kicad) that libkicad links
# against (see Config/KiCadLink.xcconfig), into build/kicad. On macOS it uses Homebrew; on Linux
# it uses the distribution packages checked by KiEMS' Linux dependency checker.
#
# Usage: Scripts/build_kicad.sh
#
# Not run from Xcode. Rerun it after the submodule moves; check_dependencies.py notices when it has.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_DIR="$REPO_ROOT/submodules/kicad"
BUILD_DIR="$REPO_ROOT/build/kicad"

if [ ! -f "$SOURCE_DIR/CMakeLists.txt" ]; then
  echo "error: $SOURCE_DIR is empty -- run: git submodule update --init submodules/kicad" >&2
  exit 1
fi

case "$(uname -s)" in
  Darwin)
    BREW="$(command -v brew || true)"
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -z "$BREW" ] && [ -x "$candidate" ] && BREW="$candidate"
    done
    if [ -z "$BREW" ]; then
      echo "error: Homebrew not found -- run Scripts/check_dependencies.py" >&2
      exit 1
    fi
    BREW_PREFIX="$("$BREW" --prefix)"
    export PATH="$BREW_PREFIX/bin:$PATH"

    # KiCad's FindngSpice looks for a Linux-style soname; point it at Homebrew's dylib explicitly.
    NGSPICE_PREFIX="$BREW_PREFIX/opt/libngspice"
    cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_PREFIX_PATH="$BREW_PREFIX" \
      -DwxWidgets_CONFIG_EXECUTABLE="$BREW_PREFIX/opt/wxwidgets@3.2/bin/wx-config-3.2" \
      -DNGSPICE_INCLUDE_DIR="$NGSPICE_PREFIX/include" \
      -DNGSPICE_LIBRARY="$NGSPICE_PREFIX/lib/libngspice.dylib" \
      -DNGSPICE_DLL="$NGSPICE_PREFIX/lib/libngspice.dylib" \
      -DOCC_INCLUDE_DIR="$BREW_PREFIX/opt/opencascade/include/opencascade" \
      -DOCC_LIBRARY_DIR="$BREW_PREFIX/opt/opencascade/lib" \
      -DKICAD_BUILD_QA_TESTS=OFF \
      -DKICAD_SPICE_QA=OFF \
      -DKICAD_BUILD_I18N=OFF \
      -DKICAD_USE_SENTRY=OFF \
      -DKICAD_UPDATE_CHECK=OFF
    ;;
  Linux)
    cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DKICAD_BUILD_QA_TESTS=OFF \
      -DKICAD_SPICE_QA=OFF \
      -DKICAD_BUILD_I18N=OFF \
      -DKICAD_WAYLAND=OFF \
      -DKICAD_USE_SENTRY=OFF \
      -DKICAD_UPDATE_CHECK=OFF
    ;;
  *)
    echo "error: unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac

# libkicad uses pcbnew internals.  Build the complete pcbnew module rather than selecting a few
# API objects: those objects depend on its importer, 3D, router, and platform implementations.
cmake --build "$BUILD_DIR" --target pcbnew_kiface

# KiCad builds pcbnew as a loadable module and intentionally hides its internal C++ symbols.
# libkicad needs those internals directly, so package the already-built object target into a
# static archive for Linux consumers.  Keep the module build above: it ensures every object and
# static dependency is present before we collect the archive.
if [ "$(uname -s)" = "Linux" ]; then
  PCBNEW_OBJECT_DIR="$BUILD_DIR/pcbnew/CMakeFiles/pcbnew_kiface_objects.dir"
  PCBNEW_MODULE_OBJECT="$BUILD_DIR/pcbnew/CMakeFiles/pcbnew_kiface.dir/pcbnew.cpp.o"
  PCBNEW_OBJECT_ARCHIVE="$BUILD_DIR/pcbnew/libpcbnew_kiface_objects.a"
  if [ ! -d "$PCBNEW_OBJECT_DIR" ] || [ ! -f "$PCBNEW_MODULE_OBJECT" ]; then
    echo "error: KiCad did not produce pcbnew object files" >&2
    exit 1
  fi
  mapfile -d '' PCBNEW_OBJECTS < <(find "$PCBNEW_OBJECT_DIR" -name '*.o' -print0)
  if [ "${#PCBNEW_OBJECTS[@]}" -eq 0 ]; then
    echo "error: KiCad produced no pcbnew object files" >&2
    exit 1
  fi
  rm -f "$PCBNEW_OBJECT_ARCHIVE"
  ar rcs "$PCBNEW_OBJECT_ARCHIVE" "$PCBNEW_MODULE_OBJECT" "${PCBNEW_OBJECTS[@]}"
  ranlib "$PCBNEW_OBJECT_ARCHIVE"
fi

# Lets check_dependencies.py notice when the submodule has moved on since this build.
git -C "$SOURCE_DIR" rev-parse HEAD > "$BUILD_DIR/.built-commit"

echo "Built KiCad libraries in $BUILD_DIR"
