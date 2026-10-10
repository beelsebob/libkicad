// Evaluates SPICE numeric values and {...} parameter expressions, so R/L/C elements whose values
// reference subcircuit parameters ("{R}", "{Cnom*1.1}") resolve to numbers. Plain C++ -- no KiCad
// headers -- like spice_subcircuit.hpp, which uses it.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <string_view>

namespace libkicad::detail {

/// Parameter values in scope, keyed by lower-case name (SPICE names are case-insensitive).
using SpiceParameters = std::map<std::string, double>;

/// Evaluates one SPICE value: a number with an optional scale suffix and unit ("10k", "4.7uF",
/// "2meg", "4k7"), or an expression -- bare, or wrapped in {...} or '...' -- over numbers,
/// `parameters`, + - * / ^ ** and parentheses, and the functions abs, sqrt, exp, log, log10, pow,
/// min and max. nullopt if `text` isn't one of those, names an unknown parameter or function, or
/// doesn't evaluate to a finite number.
std::optional<double> evaluateSpiceExpression(std::string_view text, const SpiceParameters& parameters);

/// The principal value of an R, L or C element, given everything after its terminals
/// (ComponentSimElement::value): its first token, or the right-hand side of an "r=", "l=" or "c="
/// instance parameter, evaluated with `parameters`.
std::optional<double> evaluateElementValue(char kind, std::string_view value, const SpiceParameters& parameters);

} // namespace libkicad::detail
