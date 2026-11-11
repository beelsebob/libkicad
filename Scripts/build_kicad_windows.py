#!/usr/bin/env python3
"""Windows counterpart of build_kicad.sh, for LIBKICAD_BACKEND=kicad-source only: builds the parts
of the KiCad submodule that libkicad links against into build/kicad, using KiCad's own supported
Windows toolchain (MSVC + vcpkg). The default Windows backend, stock, needs none of this -- it uses
an installed KiCad (see libkicad/stock/). This path is experimental and hasn't been run end to end.

Run it from a Visual Studio x64 Developer prompt with VCPKG_ROOT set:

    python Scripts/build_kicad_windows.py [--manifest-dir DIR] [--installed-dir DIR]

By default vcpkg installs KiCad's dependencies from KiCad's own manifest. A consumer that builds
other code against the same libraries should pass a manifest that includes KiCad's dependencies at
KiCad's vcpkg baseline as --manifest-dir, and a shared --installed-dir, so everything links against
a single dependency set.

Like the Linux path, libkicad statically links pcbnew's and eeschema's complete object targets,
because KiCad keeps their internals out of the _pcbnew/_eeschema modules' exports. This script
archives those objects with lib.exe, then records, from KiCad's own build files, exactly how KiCad
compiles and links them: include directories, preprocessor definitions, forced includes, link
libraries and the KiCad DLLs the result needs at run time. It writes them to
build/kicad/libkicad_windows.cmake, which libkicad's CMakeLists.txt includes on Windows. Deriving
the set from KiCad's build (rather than listing it by hand, as the macOS and Linux branches do)
keeps it correct as the submodule moves.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "submodules" / "kicad"
BUILD_DIR = REPO_ROOT / "build" / "kicad"
BUILD_TYPE = "RelWithDebInfo"  # What KiCad's own msvc-win64-release preset uses.
TRIPLET = "x64-windows"

# Built rather than just pcbnew_kiface_objects/eeschema_kiface_objects so that every static library
# and DLL the two modules link exists afterwards.
BUILD_TARGETS = ["pcbnew_kiface", "eeschema_kiface"]

# (CMake object-target directory, module output, archive to create, extra object to include)
ARCHIVES = [
    ("pcbnew/CMakeFiles/pcbnew_kiface_objects.dir", "_pcbnew.kiface",
     "pcbnew/pcbnew_kiface_objects.lib", "pcbnew/CMakeFiles/pcbnew_kiface.dir/pcbnew.cpp.obj"),
    # Like build_kicad.sh, deliberately omits the module's own eeschema.cpp object, which would
    # define a second Kiface().
    ("eeschema/CMakeFiles/eeschema_kiface_objects.dir", "_eeschema.kiface",
     "eeschema/eeschema_kiface_objects.lib", None),
]


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def run(command: list[str]) -> None:
    print("+ " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True)


def split_windows_command_line(line: str) -> list[str]:
    """Splits a command line the way CommandLineToArgvW does (the rules cl.exe and link.exe use)."""
    arguments: list[str] = []
    current: list[str] = []
    in_quotes = False
    have_argument = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            j = i
            while j < len(line) and line[j] == "\\":
                j += 1
            count = j - i
            if j < len(line) and line[j] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    j += 1
                i = j
            else:
                current.append("\\" * count)
                i = j
            have_argument = True
            continue
        if c == '"':
            in_quotes = not in_quotes
            have_argument = True
        elif c in " \t" and not in_quotes:
            if have_argument:
                arguments.append("".join(current))
                current = []
                have_argument = False
        else:
            current.append(c)
            have_argument = True
        i += 1
    if have_argument:
        arguments.append("".join(current))
    return arguments


def absolute(path: str, base: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve().as_posix()


def configure_and_build(args: argparse.Namespace) -> None:
    vcpkg_root = os.environ.get("VCPKG_ROOT")
    if not vcpkg_root:
        fail("VCPKG_ROOT is not set; install vcpkg and set VCPKG_ROOT (see the KiEMS README)")
    if not shutil.which("cl"):
        fail("cl.exe is not on PATH; run this from a Visual Studio x64 Developer prompt")
    if not (SOURCE_DIR / "CMakeLists.txt").is_file():
        fail(f"{SOURCE_DIR} is empty -- run: git submodule update --init submodules/kicad")

    command = [
        "cmake", "-S", str(SOURCE_DIR), "-B", str(BUILD_DIR), "-G", "Ninja",
        f"-DCMAKE_BUILD_TYPE={BUILD_TYPE}",
        f"-DCMAKE_TOOLCHAIN_FILE={Path(vcpkg_root) / 'scripts' / 'buildsystems' / 'vcpkg.cmake'}",
        f"-DVCPKG_TARGET_TRIPLET={TRIPLET}",
        "-DCMAKE_C_COMPILER=cl", "-DCMAKE_CXX_COMPILER=cl",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        "-DKICAD_BUILD_QA_TESTS=OFF",
        "-DKICAD_SPICE_QA=OFF",
        "-DKICAD_BUILD_I18N=OFF",
        "-DKICAD_USE_SENTRY=OFF",
        "-DKICAD_UPDATE_CHECK=OFF",
        "-DKICAD_WIN32_DPI_AWARE=ON",
    ]
    if args.manifest_dir:
        command.append(f"-DVCPKG_MANIFEST_DIR={Path(args.manifest_dir).resolve()}")
    if args.installed_dir:
        command.append(f"-DVCPKG_INSTALLED_DIR={Path(args.installed_dir).resolve()}")
    run(command)
    run(["cmake", "--build", str(BUILD_DIR), "--target", *BUILD_TARGETS])


def create_archives() -> None:
    librarian = shutil.which("lib")
    if not librarian:
        fail("lib.exe is not on PATH; run this from a Visual Studio x64 Developer prompt")
    for object_dir, _, archive, extra_object in ARCHIVES:
        objects = sorted((BUILD_DIR / object_dir).rglob("*.obj"))
        if not objects:
            fail(f"KiCad produced no object files in {BUILD_DIR / object_dir}")
        if extra_object:
            extra = BUILD_DIR / extra_object
            if not extra.is_file():
                fail(f"KiCad did not produce {extra}")
            objects.append(extra)
        output = BUILD_DIR / archive
        output.unlink(missing_ok=True)
        # Thousands of objects overflow the command line; lib.exe reads them from a response file.
        response = output.with_suffix(".rsp")
        response.write_text("\n".join(f'"{o}"' for o in objects) + "\n", encoding="utf-8")
        run([librarian, "/NOLOGO", f"/OUT:{output}", f"@{response}"])


def compile_flags(entries: list[dict], object_dir: str) -> tuple[list[str], list[str], list[str]]:
    """Include directories, definitions and forced includes of one TU in `object_dir`."""
    marker = object_dir.replace("\\", "/")
    for entry in entries:
        output = entry.get("output", "").replace("\\", "/")
        if marker in output and entry.get("file", "").endswith((".cpp", ".cxx")):
            break
    else:
        fail(f"compile_commands.json has no translation unit for {object_dir}")
    arguments = entry["arguments"] if "arguments" in entry else split_windows_command_line(entry["command"])
    directory = Path(entry["directory"])
    includes: list[str] = []
    definitions: list[str] = []
    forced: list[str] = []
    for argument in arguments:
        for prefix, bucket in (("-external:I", includes), ("/external:I", includes), ("-I", includes),
                               ("/I", includes), ("-D", definitions), ("/D", definitions),
                               ("-FI", forced), ("/FI", forced)):
            if argument.startswith(prefix) and len(argument) > len(prefix):
                value = argument[len(prefix):]
                if bucket is not definitions:
                    value = absolute(value, directory)
                if value not in bucket:
                    bucket.append(value)
                break
    return includes, definitions, forced


def link_libraries(build_ninja: str, module_output: str) -> list[str]:
    """LINK_LIBRARIES of the ninja build statement that produces `module_output`."""
    lines = build_ninja.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("build "):
            continue
        outputs = line[len("build "):].split(":", 1)[0].replace("$ ", " ").replace("\\", "/")
        if not re.search(r"(^|[ /])" + re.escape(module_output) + r"($| )", outputs):
            continue
        for variable in lines[index + 1:]:
            if not variable.startswith((" ", "\t")):
                break
            name, _, value = variable.strip().partition(" = ")
            if name == "LINK_LIBRARIES":
                return split_windows_command_line(value.replace("$$", "$"))
        return []
    fail(f"build.ninja has no build statement for {module_output}")
    return []


def write_link_description() -> None:
    compile_commands = BUILD_DIR / "compile_commands.json"
    build_ninja_path = BUILD_DIR / "build.ninja"
    if not compile_commands.is_file() or not build_ninja_path.is_file():
        fail(f"{BUILD_DIR} is not a Ninja KiCad build with compile_commands.json")
    entries = json.loads(compile_commands.read_text(encoding="utf-8"))
    build_ninja = build_ninja_path.read_text(encoding="utf-8", errors="replace")

    pcbnew_includes, pcbnew_definitions, pcbnew_forced = compile_flags(entries, ARCHIVES[0][0])
    eeschema_includes, eeschema_definitions, eeschema_forced = compile_flags(entries, ARCHIVES[1][0])

    libraries: list[str] = []
    for _, module_output, _, _ in ARCHIVES:
        for library in link_libraries(build_ninja, module_output):
            if library.startswith(("/", "-")) and not Path(library).is_absolute():
                continue  # A linker option, not a library.
            if (BUILD_DIR / library).exists() or Path(library).is_absolute():
                library = absolute(library, BUILD_DIR)
            if library not in libraries:
                libraries.append(library)

    # KiCad's own DLLs (kicommon, kigal, kiapi, kicad_3dsg, ...): an import library in the build
    # tree with a DLL of the same name beside it, or anywhere else in the build tree.
    dlls: dict[str, str] = {}
    all_dlls = {path.stem.lower(): path for path in BUILD_DIR.rglob("*.dll")
                if "vcpkg_installed" not in path.parts}
    for library in libraries:
        path = Path(library)
        if path.suffix.lower() == ".lib" and path.is_absolute() and BUILD_DIR.resolve().as_posix() in path.as_posix():
            dll = all_dlls.get(path.stem.lower())
            if dll is not None:
                dlls[library] = dll.resolve().as_posix()

    archives = [(BUILD_DIR / archive).resolve().as_posix() for _, _, archive, _ in ARCHIVES]

    def cmake_list(values: list[str]) -> str:
        for value in values:
            if "]==]" in value or ";" in value:
                fail(f"cannot express {value!r} as a CMake list element")
        return "[==[" + ";".join(values) + "]==]"

    lines = [
        "# Generated by Scripts/build_kicad_windows.py from KiCad's build files. Do not edit.",
        f"set(LIBKICAD_WINDOWS_KICAD_COMMIT {cmake_list([commit()])})",
        f"set(LIBKICAD_WINDOWS_BUILD_TYPE {BUILD_TYPE})",
        f"set(LIBKICAD_WINDOWS_PCBNEW_INCLUDES {cmake_list(pcbnew_includes)})",
        f"set(LIBKICAD_WINDOWS_PCBNEW_DEFINITIONS {cmake_list(pcbnew_definitions)})",
        f"set(LIBKICAD_WINDOWS_PCBNEW_FORCED_INCLUDES {cmake_list(pcbnew_forced)})",
        f"set(LIBKICAD_WINDOWS_EESCHEMA_INCLUDES {cmake_list(eeschema_includes)})",
        f"set(LIBKICAD_WINDOWS_EESCHEMA_DEFINITIONS {cmake_list(eeschema_definitions)})",
        f"set(LIBKICAD_WINDOWS_EESCHEMA_FORCED_INCLUDES {cmake_list(eeschema_forced)})",
        f"set(LIBKICAD_WINDOWS_OBJECT_ARCHIVES {cmake_list(archives)})",
        f"set(LIBKICAD_WINDOWS_LINK_LIBRARIES {cmake_list([l for l in libraries if l not in dlls])})",
        f"set(LIBKICAD_WINDOWS_IMPORT_LIBRARIES {cmake_list(list(dlls.keys()))})",
        f"set(LIBKICAD_WINDOWS_RUNTIME_DLLS {cmake_list(list(dlls.values()))})",
        "",
    ]
    (BUILD_DIR / "libkicad_windows.cmake").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {BUILD_DIR / 'libkicad_windows.cmake'}: {len(libraries)} link libraries, {len(dlls)} KiCad DLLs")


def commit() -> str:
    return subprocess.run(["git", "-C", str(SOURCE_DIR), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    if sys.platform != "win32":
        fail("this script builds KiCad for Windows; on macOS and Linux use Scripts/build_kicad.sh")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest-dir", help="vcpkg manifest directory (default: KiCad's own)")
    parser.add_argument("--installed-dir", help="vcpkg installed tree to share with the consuming build")
    parser.add_argument("--skip-build", action="store_true",
                        help="only re-create the archives and libkicad_windows.cmake")
    args = parser.parse_args()

    if not args.skip_build:
        configure_and_build(args)
    create_archives()
    write_link_description()
    # Lets check_dependencies.py notice when the submodule has moved on since this build.
    (BUILD_DIR / ".built-commit").write_text(commit() + "\n", encoding="utf-8")
    print(f"Built KiCad libraries in {BUILD_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
