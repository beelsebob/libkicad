# libkicad

An in-process, headless C++ API for reading KiCad boards. Rather than shelling out to
`kicad-cli` or talking to a running KiCad over IPC, libkicad links the relevant parts of KiCad itself
and drives the same `API_HANDLER_PCB` command path that KiCad's IPC API server uses — without a
`PCB_EDIT_FRAME`, `KIWAY`, or any wx window.

It is built as a static library (`liblibkicad.a`) and is used by [KiEMS](https://github.com/beelsebob/kiems).

## What it gives you

Given a `.kicad_pro` and `.kicad_pcb`, a `libkicad::Board` answers:

- **Nets and net classes** — all nets, nets in a class, a net's effective class, net colour overrides
- **Pads and pins** — pads on a net, every pad, resolving a footprint + pin (by pad number or pin
  function) to its net and position, footprints with their pins
- **Copper** — tracks (per net or board-wide), zones, plated through-holes and vias, non-plated holes
- **Geometry** — board outline, net-owned copper, solder-mask openings, silkscreen, per-layer geometry,
  bounds
- **Board setup** — physical stackup (thickness, εr, loss tangent), enabled layers, layer colours
- **3D models** — placed component models as a coloured triangle mesh, via KiCad's own STEP exporter

Coordinates are in millimetres relative to the board's auxiliary origin (the frame `kicad-cli` uses
with `--use-drill-file-origin`).

## Usage

```cpp
#include "libkicad.hpp"

auto runtime = libkicad::Runtime::create();   // once per process; Cocoa apps: on the main thread
if (!runtime) { /* runtime.error() */ }

libkicad::Board board(*runtime, "board.kicad_pro", "board.kicad_pcb");

if (auto pads = board.padsOnNet("GND")) {
    for (const auto& pad : *pads)
        printf("%s.%s at (%g, %g) on %s\n", pad.footprintRef.c_str(), pad.padNumber.c_str(),
               pad.xMm, pad.yMm, pad.copperLayerName.c_str());
}
```

Every query returns `std::expected<T, std::string>`. Queries are thread-safe: board access is
serialized by the `Runtime`, which must outlive every `Board` opened from it. A `Board` loads its files
eagerly and reloads automatically if either file's mtime changes. KiCad only keeps one active project,
so loading a board from another project unloads the previous one until its next query.

The public header requires C++23. It must not be included in a translation unit that also includes
KiCad headers, which only compile at C++20; `libkicad_result.hpp` holds the plain data types shared
across that boundary.

## Requirements

- macOS and Xcode
- Homebrew (boost, wxwidgets@3.2, opencascade, cmake, ninja, protobuf, … — see
  `Scripts/check_dependencies.py` for the full list and expected versions)
- The KiCad source, as the `submodules/kicad` submodule

## Building

```sh
git clone --recurse-submodules https://github.com/beelsebob/libkicad
cd libkicad
Scripts/check_dependencies.py
```

`check_dependencies.py` checks the Homebrew formulas, the KiCad submodule and the KiCad build, offering
to install or build whatever is missing (`--yes` for unattended runs, `--ssh` to fetch GitHub submodules
over ssh). Until it succeeds, every Xcode compile stops with an `#error` from `Config/DependencyCheck.h`.

It runs `Scripts/build_kicad.sh` when needed, which builds only the KiCad libraries and objects
libkicad links into `build/kicad`. This is slow and is never run from Xcode; rerun the check after
moving the KiCad submodule.

`libkicad` is built by CMake. The Xcode project remains as a convenient source
navigator and a thin build target for projects that retain an Xcode workflow;
its **libkicad** target invokes the same CMake build and writes `liblibkicad.a`
to Xcode's built-products directory.

On macOS, either build that target in Xcode or use a preset directly:

```sh
cmake --preset macos-debug
cmake --build --preset macos-debug
```

The equivalent Linux presets are `linux-debug` and `linux-release`. They expect
a matching KiCad source build at `build/kicad`; KiEMS' Linux dependency checker
creates it automatically, or a standalone checkout can run
`Scripts/build_kicad.sh` after installing its distribution dependencies.
Override `KICAD_SOURCE_ROOT`, `KICAD_BUILD_ROOT`, and (when required)
`LIBKICAD_DEPENDENCY_PREFIX` when configuring for a non-default layout.

All C++ implementation files under `libkicad/` are discovered by CMake. Xcode's
file-system-synchronised source group uses the same directory, so adding a new
source file there from Xcode is picked up by both build systems on the next
build.

## Using it from another project

1. Add libkicad as a submodule and add `libkicad.xcodeproj` to your project; link `liblibkicad.a`.
2. Base each target that links it on `Config/KiCadLink.xcconfig` (or `#include` it), with
   `LIBKICAD_ROOT` set to your libkicad checkout. Because libkicad is a static archive, this carries
   the KiCad, wxWidgets and OpenCASCADE libraries the final executable needs.
3. Have your own dependency check run libkicad's — see `Scripts/dependency_tool.py`.

Paths to KiCad and Homebrew live in `Config/KiCadPaths.xcconfig`; override them in an untracked
`Config/KiCadPaths.local.xcconfig`.

## Layout

| Path | Contents |
| --- | --- |
| `libkicad/libkicad.hpp` | Public API (`Runtime`, `Board`) |
| `libkicad/libkicad_result.hpp` | Plain data types returned by queries |
| `libkicad/libkicad.cpp` | KiCad-side implementation (C++20) |
| `libkicad/libkicad_api.cpp` | Public API wrapper (C++23) |
| `libkicad/step_export/` | Component 3D model export, adapted from KiCad's STEP exporter |
| `Config/` | xcconfigs for building and linking |
| `Scripts/` | Dependency check and KiCad build |
| `submodules/kicad` | KiCad source |
