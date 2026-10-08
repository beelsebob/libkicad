// Expands a SPICE subcircuit definition into the primitive elements it instantiates. Plain C++ --
// no KiCad headers -- so the eeschema-side model resolution (eeschema/schematic_sim_models.cpp)
// and tests can share it.
#pragma once

#include "libkicad_result.hpp"

#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace libkicad::detail {

/// Returns the complete ".subckt NAME ... .ends" text of the named subcircuit from the library the
/// outer model came from, or nullopt if that library has no such subcircuit. Names are matched
/// case-insensitively, as SPICE does.
using SubcircuitLookup = std::function<std::optional<std::string>(const std::string& name)>;

/// Expands one subcircuit definition -- `spiceCode` holds its ".subckt NAME port... / body / .ends"
/// block, exactly as SIM_MODEL_SUBCKT::GetSpiceCode() returns it -- connected to `ports` (the outer
/// node on each of its ports, in header order). Nested X instances resolve against definitions
/// nested in an enclosing subcircuit first, then `lookup`; one that resolves nowhere is kept as an
/// unexpanded 'X' element. Every element and internal node name is prefixed with `instanceName`.
///
/// Comments, '+' continuation lines and dot-cards other than nested .subckt/.ends are skipped.
/// Returns nullopt, with `error` set, only if `spiceCode` holds no subcircuit definition at all.
std::optional<std::vector<ComponentSimElement>> expandSubcircuit(const std::string& spiceCode,
        const std::string& instanceName, const std::vector<ComponentSimNode>& ports,
        const SubcircuitLookup& lookup, std::string& error);

} // namespace libkicad::detail
