// The schematic half of Board::componentSimModels(). Its implementation is the only libkicad code
// built against eeschema's headers, which need a different set of compile definitions and include
// paths from the pcbnew-facing rest of libkicad -- see CMakeLists.txt's libkicad_eeschema target.
// This header deliberately includes no KiCad headers so libkicad.cpp can call across.
#pragma once

#include "libkicad_result.hpp"

#include <string>
#include <vector>

class PROJECT;

namespace libkicad::detail {

struct SchematicSimModels {
    bool ok = false;
    std::string error;
    /// One entry per non-power symbol reference, in sheet order. NoSymbol never appears here.
    std::vector<ComponentSimModel> models;
};

/// Loads the already-loaded `project`'s schematic -- the top-level sheets its project file lists,
/// or else `rootPath`, each with its hierarchy -- and resolves every symbol's simulation model. The
/// caller must hold the runtime lock for KiCad's process-wide state; the schematic is released
/// before this returns.
SchematicSimModels readSchematicSimModels(PROJECT& project, const std::string& rootPath);

} // namespace libkicad::detail
