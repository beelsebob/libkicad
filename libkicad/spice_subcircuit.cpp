#include "spice_subcircuit.hpp"

#include "spice_expression.hpp"

#include <algorithm>
#include <cctype>
#include <deque>
#include <map>
#include <sstream>
#include <utility>

namespace libkicad::detail {
namespace {

/// Bounds nested expansion, so a library whose subcircuits instantiate each other can't recurse
/// forever.
constexpr int kMaxSubcircuitDepth = 32;

bool _isSpace(char c) {
    return std::isspace(static_cast<unsigned char>(c)) != 0;
}

std::string _lower(std::string text) {
    std::ranges::transform(text, text.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return text;
}

std::string _join(const std::vector<std::string>& tokens, std::size_t first) {
    std::string joined;
    for (std::size_t i = first; i < tokens.size(); ++i) {
        if (!joined.empty()) joined += ' ';
        joined += tokens[i];
    }
    return joined;
}

/// Splits one logical line on whitespace, keeping {...} expressions and quoted strings whole and
/// dropping whitespace around '=' so "W = 1u" is the single token "W=1u".
std::vector<std::string> _tokenize(const std::string& line) {
    std::vector<std::string> tokens;
    std::string current;
    int braceDepth = 0;
    char quote = 0;
    bool afterEquals = false;
    for (char c : line) {
        if (quote != 0 || braceDepth > 0) {
            current += c;
            if (quote != 0 && c == quote) {
                quote = 0;
            } else if (quote == 0 && c == '{') {
                ++braceDepth;
            } else if (quote == 0 && c == '}') {
                --braceDepth;
            }
            continue;
        }
        if (_isSpace(c)) {
            if (!current.empty() && !afterEquals) {
                tokens.push_back(std::move(current));
                current.clear();
            }
            continue;
        }
        if (c == '=' && current.empty() && !tokens.empty()) {
            // "W =": reopen the previous token.
            current = std::move(tokens.back());
            tokens.pop_back();
        }
        afterEquals = c == '=';
        current += c;
        if (c == '{') {
            braceDepth = 1;
        } else if (c == '\'' || c == '"') {
            quote = c;
        }
    }
    if (!current.empty()) tokens.push_back(std::move(current));
    return tokens;
}

/// SPICE source as tokenized logical lines: comments and blank lines removed, '+' continuations
/// joined onto the line they continue.
std::vector<std::vector<std::string>> _logicalLines(const std::string& code) {
    std::vector<std::string> lines;
    std::istringstream stream(code);
    for (std::string line; std::getline(stream, line);) {
        // Inline comments: ';' anywhere, '$' after whitespace (the PSpice/ngspice convention).
        std::size_t end = line.find(';');
        for (std::size_t dollar = line.find('$'); dollar < end; dollar = line.find('$', dollar + 1)) {
            if (dollar == 0 || _isSpace(line[dollar - 1])) {
                end = dollar;
                break;
            }
        }
        line.erase(std::min(end, line.size()));
        line.erase(line.begin(), std::ranges::find_if_not(line, _isSpace));
        while (!line.empty() && _isSpace(line.back())) line.pop_back();
        if (line.empty() || line[0] == '*') continue;
        if (line[0] == '+') {
            if (!lines.empty()) lines.back() += " " + line.substr(1);
            continue;
        }
        lines.push_back(std::move(line));
    }

    std::vector<std::vector<std::string>> tokenized;
    for (const std::string& line : lines) {
        std::vector<std::string> tokens = _tokenize(line);
        if (!tokens.empty()) tokenized.push_back(std::move(tokens));
    }
    return tokenized;
}

bool _isParameter(const std::string& token) {
    return token.find('=') != std::string::npos || _lower(token).starts_with("params:");
}

/// The name=value parameters among `tokens[first...]`, with names lower-cased and any "params:"
/// marker (alone, or glued to the first parameter) skipped.
std::vector<std::pair<std::string, std::string>> _parameters(const std::vector<std::string>& tokens,
                                                             std::size_t first) {
    std::vector<std::pair<std::string, std::string>> parameters;
    for (std::size_t i = first; i < tokens.size(); ++i) {
        std::string token = tokens[i];
        if (_lower(token).starts_with("params:")) token.erase(0, 7);
        const std::size_t equals = token.find('=');
        if (equals == std::string::npos || equals == 0) continue;
        parameters.emplace_back(_lower(token.substr(0, equals)), token.substr(equals + 1));
    }
    return parameters;
}

struct Definition {
    std::string name; // lower-case, as are the ports
    std::vector<std::string> ports;
    /// The header's parameter defaults, in order: name (lower-case) and value expression.
    std::vector<std::pair<std::string, std::string>> parameters;
    /// The body's .param cards, in order.
    std::vector<std::pair<std::string, std::string>> parameterCards;
    std::vector<std::vector<std::string>> elements;
    std::vector<Definition> nested;
};

/// Parses the .subckt block starting at `lines[index]`, leaving `index` just past its .ends (or at
/// the end of the input, for a definition missing one).
Definition _parseDefinition(const std::vector<std::vector<std::string>>& lines, std::size_t& index) {
    const std::vector<std::string>& header = lines[index++];
    Definition definition;
    if (header.size() > 1) definition.name = _lower(header[1]);
    std::size_t i = 2;
    for (; i < header.size() && !_isParameter(header[i]); ++i) {
        definition.ports.push_back(_lower(header[i]));
    }
    definition.parameters = _parameters(header, i);

    while (index < lines.size()) {
        const std::string card = _lower(lines[index][0]);
        if (card == ".subckt") {
            definition.nested.push_back(_parseDefinition(lines, index));
            continue;
        }
        ++index;
        if (card.starts_with(".ends")) break; // .ends, or the .endsubckt some dialects use
        if (card == ".param") {
            for (auto& parameter : _parameters(lines[index - 1], 1)) {
                definition.parameterCards.push_back(std::move(parameter));
            }
            continue;
        }
        // .model/.param/.func and friends parameterize elements but aren't elements themselves.
        if (card[0] != '.') definition.elements.push_back(lines[index - 1]);
    }
    return definition;
}

std::optional<Definition> _parseFirstDefinition(const std::string& code) {
    const std::vector<std::vector<std::string>> lines = _logicalLines(code);
    for (std::size_t index = 0; index < lines.size(); ++index) {
        if (_lower(lines[index][0]) == ".subckt") return _parseDefinition(lines, index);
    }
    return std::nullopt;
}

class Expander {
public:
    Expander(const SubcircuitLookup& lookup, std::vector<ComponentSimElement>& elements)
            : _lookup(lookup), _elements(elements) {}

    /// `scope` holds the enclosing definitions whose nested .subckt blocks are visible here;
    /// `overrides` the instance's parameter values, which replace the header's defaults.
    void expand(const Definition& definition, const std::string& prefix,
                const std::map<std::string, ComponentSimNode>& portNodes,
                std::vector<const Definition*> scope, const SpiceParameters& overrides, int depth) {
        scope.push_back(&definition);
        // A parameter that can't be evaluated stays unset, so only the elements using it lose
        // their numeric values.
        SpiceParameters parameters;
        for (const auto& [name, expression] : definition.parameters) {
            if (auto value = evaluateSpiceExpression(expression, parameters)) parameters[name] = *value;
        }
        for (const auto& [name, value] : overrides) parameters[name] = value;
        for (const auto& [name, expression] : definition.parameterCards) {
            if (auto value = evaluateSpiceExpression(expression, parameters)) {
                parameters[name] = *value;
            } else {
                parameters.erase(name);
            }
        }

        auto node = [&](const std::string& token) {
            const std::string key = _lower(token);
            if (key == "0") return ComponentSimNode{ComponentSimNode::Kind::Ground, "0"};
            if (auto port = portNodes.find(key); port != portNodes.end()) return port->second;
            return ComponentSimNode{ComponentSimNode::Kind::Internal, prefix + "." + key};
        };

        for (const std::vector<std::string>& tokens : definition.elements) {
            ComponentSimElement element;
            element.kind = static_cast<char>(std::toupper(static_cast<unsigned char>(tokens[0][0])));
            element.name = prefix + "." + tokens[0];

            if (element.kind == 'R' || element.kind == 'C' || element.kind == 'L') {
                for (std::size_t i = 1; i < std::min<std::size_t>(3, tokens.size()); ++i) {
                    element.nodes.push_back(node(tokens[i]));
                }
                element.value = _join(tokens, 3);
                element.numericValue = evaluateElementValue(element.kind, element.value, parameters);
            } else if (element.kind == 'X') {
                // Xname node... subcircuit [params: ...|name=value ...]: the subcircuit name is the
                // last token before the parameters.
                std::size_t firstParameter = 1;
                while (firstParameter < tokens.size() && !_isParameter(tokens[firstParameter])) ++firstParameter;
                if (firstParameter < 3) {
                    element.value = "malformed subcircuit instance: " + _join(tokens, 1);
                    _elements.push_back(std::move(element));
                    continue;
                }
                const std::string& subcircuitName = tokens[firstParameter - 1];
                for (std::size_t i = 1; i + 1 < firstParameter; ++i) element.nodes.push_back(node(tokens[i]));

                const auto [nested, nestedScope] = _resolve(subcircuitName, scope);
                if (!nested) {
                    element.value = "subcircuit " + subcircuitName + " not found";
                } else if (nested->ports.size() != element.nodes.size()) {
                    element.value = "subcircuit " + subcircuitName + " has " + std::to_string(nested->ports.size()) +
                                    " ports but is instantiated with " + std::to_string(element.nodes.size());
                } else if (depth >= kMaxSubcircuitDepth) {
                    element.value = "subcircuit " + subcircuitName + " is nested too deeply";
                } else {
                    std::map<std::string, ComponentSimNode> nestedPorts;
                    for (std::size_t i = 0; i < nested->ports.size(); ++i) {
                        nestedPorts.emplace(nested->ports[i], element.nodes[i]);
                    }
                    SpiceParameters nestedOverrides;
                    for (const auto& [name, expression] : _parameters(tokens, firstParameter)) {
                        if (auto value = evaluateSpiceExpression(expression, parameters)) {
                            nestedOverrides[name] = *value;
                        }
                    }
                    expand(*nested, element.name, nestedPorts, nestedScope, nestedOverrides, depth + 1);
                    continue;
                }
            } else {
                // Terminal counts vary (and some are optional) for every other element type.
                element.value = _join(tokens, 1);
            }
            _elements.push_back(std::move(element));
        }
    }

private:
    /// The definition `name` refers to from `scope`, and the scope to expand it in: a definition
    /// nested in an enclosing subcircuit keeps seeing that subcircuit's siblings; a library-level
    /// one starts afresh.
    std::pair<const Definition*, std::vector<const Definition*>> _resolve(
            const std::string& name, const std::vector<const Definition*>& scope) {
        const std::string key = _lower(name);
        for (std::size_t level = scope.size(); level-- > 0;) {
            for (const Definition& candidate : scope[level]->nested) {
                if (candidate.name == key) {
                    return {&candidate, std::vector<const Definition*>(scope.begin(), scope.begin() + level + 1)};
                }
            }
        }

        auto cached = _library.find(key);
        if (cached == _library.end()) {
            const Definition* definition = nullptr;
            if (std::optional<std::string> code = _lookup ? _lookup(name) : std::nullopt) {
                if (std::optional<Definition> parsed = _parseFirstDefinition(*code)) {
                    definition = &_libraryDefinitions.emplace_back(std::move(*parsed));
                }
            }
            cached = _library.emplace(key, definition).first;
        }
        return {cached->second, {}};
    }

    const SubcircuitLookup& _lookup;
    std::vector<ComponentSimElement>& _elements;
    /// Library definitions parsed so far, by lower-case name; nullptr caches "not in the library".
    /// The deque keeps their addresses stable while later lookups append.
    std::deque<Definition> _libraryDefinitions;
    std::map<std::string, const Definition*> _library;
};

} // namespace

std::optional<std::vector<ComponentSimElement>> expandSubcircuit(const std::string& spiceCode,
        const std::string& instanceName, const std::vector<ComponentSimNode>& ports,
        const SubcircuitLookup& lookup, std::string& error, const SpiceParameters& parameters) {
    std::optional<Definition> definition = _parseFirstDefinition(spiceCode);
    if (!definition) {
        error = "The model has no .subckt definition";
        return std::nullopt;
    }

    // A port the caller has no outer node for (an unconnected model pin) falls through to an
    // ordinary internal node.
    std::map<std::string, ComponentSimNode> portNodes;
    for (std::size_t i = 0; i < std::min(ports.size(), definition->ports.size()); ++i) {
        portNodes.emplace(definition->ports[i], ports[i]);
    }

    std::vector<ComponentSimElement> elements;
    Expander(lookup, elements).expand(*definition, instanceName, portNodes, {}, parameters, 0);
    return elements;
}

} // namespace libkicad::detail
