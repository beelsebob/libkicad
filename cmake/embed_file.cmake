# Embeds a file in C++ as a byte array:  cmake -DINPUT=<file> -DOUTPUT=<header> -DNAME=<symbol> -P embed_file.cmake
# The header defines <NAME> (unsigned char[]) and <NAME>Size. A byte array, unlike a string
# literal, has no compiler length limit (MSVC caps string literals at 64 KiB).
file(READ "${INPUT}" content HEX)
string(LENGTH "${content}" hexLength)
math(EXPR size "${hexLength} / 2")
string(REGEX REPLACE "([0-9a-f][0-9a-f])" "0x\\1," bytes "${content}")
# Wrap the initialiser so the generated header stays readable in a debugger.
string(REGEX REPLACE "((0x[0-9a-f][0-9a-f],){24})" "\\1\n    " bytes "${bytes}")
file(WRITE "${OUTPUT}.tmp"
    "// Generated from ${INPUT} by embed_file.cmake. Do not edit.\n"
    "#pragma once\n#include <cstddef>\n\n"
    "inline constexpr unsigned char ${NAME}[] = {\n    ${bytes}\n};\n"
    "inline constexpr std::size_t ${NAME}Size = ${size};\n")
file(COPY_FILE "${OUTPUT}.tmp" "${OUTPUT}" ONLY_IF_DIFFERENT)
file(REMOVE "${OUTPUT}.tmp")
