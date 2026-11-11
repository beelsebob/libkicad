// Runs a helper process to completion for libkicad's stock-KiCad backend. Kept in its own
// translation unit so <windows.h> stays out of everything else.
#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace libkicad::detail {

/// Runs `executable` with `arguments` (UTF-8), with no console window and with its standard
/// output and error discarded, and waits for it. Returns its exit status, or -1 with `error` set
/// if it could not be started.
int runProcess(const std::filesystem::path& executable, const std::vector<std::string>& arguments,
               std::string& error);

} // namespace libkicad::detail
