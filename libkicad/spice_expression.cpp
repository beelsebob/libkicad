#include "spice_expression.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <vector>

namespace libkicad::detail {
namespace {

bool _isSpace(char c) {
    return std::isspace(static_cast<unsigned char>(c)) != 0;
}

bool _isDigit(char c) {
    return std::isdigit(static_cast<unsigned char>(c)) != 0;
}

bool _isIdentifierStart(char c) {
    return std::isalpha(static_cast<unsigned char>(c)) != 0 || c == '_';
}

bool _isIdentifierPart(char c) {
    return std::isalnum(static_cast<unsigned char>(c)) != 0 || c == '_' || c == '.';
}

char _lower(char c) {
    return static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
}

std::string _lower(std::string_view text) {
    std::string lowered;
    for (char c : text) lowered += _lower(c);
    return lowered;
}

/// The SPICE scale factor `text` starts with ("meg" before "m"), and how many characters it used.
/// Anything after the scale is a unit, which SPICE ignores ("10kOhm", "1uF").
std::pair<double, std::size_t> _scale(std::string_view text) {
    const std::string lowered = _lower(text.substr(0, 3));
    if (lowered.starts_with("meg")) return {1e6, 3};
    if (lowered.starts_with("mil")) return {25.4e-6, 3};
    // KiCad writes micro as the UTF-8 micro sign or Greek mu.
    if (text.starts_with("\xC2\xB5") || text.starts_with("\xCE\xBC")) return {1e-6, 2};
    if (lowered.empty()) return {1, 0};
    switch (lowered[0]) {
    case 't': return {1e12, 1};
    case 'g': return {1e9, 1};
    case 'k': return {1e3, 1};
    case 'm': return {1e-3, 1};
    case 'u': return {1e-6, 1};
    case 'n': return {1e-9, 1};
    case 'p': return {1e-12, 1};
    case 'f': return {1e-15, 1};
    case 'a': return {1e-18, 1};
    default: return {1, 0};
    }
}

class Parser {
public:
    Parser(std::string_view text, const SpiceParameters& parameters) : _text(text), _parameters(parameters) {}

    std::optional<double> parse() {
        std::optional<double> value = _expression();
        _skipSpace();
        if (!value || _position != _text.size() || !std::isfinite(*value)) return std::nullopt;
        return value;
    }

private:
    void _skipSpace() {
        while (_position < _text.size() && _isSpace(_text[_position])) ++_position;
    }

    bool _consume(std::string_view token) {
        _skipSpace();
        if (!_text.substr(_position).starts_with(token)) return false;
        _position += token.size();
        return true;
    }

    std::optional<double> _expression() {
        std::optional<double> value = _term();
        while (value) {
            if (_consume("+")) {
                const std::optional<double> rhs = _term();
                value = rhs ? std::optional(*value + *rhs) : std::nullopt;
            } else if (_consume("-")) {
                const std::optional<double> rhs = _term();
                value = rhs ? std::optional(*value - *rhs) : std::nullopt;
            } else {
                break;
            }
        }
        return value;
    }

    std::optional<double> _term() {
        std::optional<double> value = _unary();
        while (value) {
            _skipSpace();
            // "**" is a power, not two multiplications.
            if (_text.substr(_position).starts_with("**")) break;
            if (_consume("*")) {
                const std::optional<double> rhs = _unary();
                value = rhs ? std::optional(*value * *rhs) : std::nullopt;
            } else if (_consume("/")) {
                const std::optional<double> rhs = _unary();
                value = rhs ? std::optional(*value / *rhs) : std::nullopt;
            } else {
                break;
            }
        }
        return value;
    }

    std::optional<double> _unary() {
        if (_consume("-")) {
            const std::optional<double> value = _unary();
            return value ? std::optional(-*value) : std::nullopt;
        }
        if (_consume("+")) return _unary();
        return _power();
    }

    /// Right-associative, binding tighter than unary minus on its left ("-2^2" is -4) but taking
    /// one on its right ("2^-1").
    std::optional<double> _power() {
        std::optional<double> base = _primary();
        if (base && (_consume("**") || _consume("^"))) {
            const std::optional<double> exponent = _unary();
            return exponent ? std::optional(std::pow(*base, *exponent)) : std::nullopt;
        }
        return base;
    }

    std::optional<double> _primary() {
        _skipSpace();
        if (_position >= _text.size()) return std::nullopt;
        const char c = _text[_position];
        if (c == '(' || c == '{') {
            ++_position;
            std::optional<double> value = _expression();
            return _consume(c == '(' ? ")" : "}") ? value : std::nullopt;
        }
        if (_isDigit(c) || c == '.') return _number();
        if (_isIdentifierStart(c)) return _identifier();
        return std::nullopt;
    }

    std::optional<double> _number() {
        const std::size_t start = _position;
        while (_position < _text.size() && (_isDigit(_text[_position]) || _text[_position] == '.')) ++_position;
        std::string mantissa(_text.substr(start, _position - start));
        if (_position + 1 < _text.size() && _lower(_text[_position]) == 'e' &&
            (_isDigit(_text[_position + 1]) ||
             ((_text[_position + 1] == '+' || _text[_position + 1] == '-') && _position + 2 < _text.size() &&
              _isDigit(_text[_position + 2])))) {
            const std::size_t exponentStart = _position;
            _position += 2;
            while (_position < _text.size() && _isDigit(_text[_position])) ++_position;
            mantissa += _text.substr(exponentStart, _position - exponentStart);
        }

        const auto [scale, scaleLength] = _scale(_text.substr(_position));
        _position += scaleLength;
        // "4k7": digits after the scale continue the mantissa, as in the RKM code.
        if (scaleLength > 0 && mantissa.find_first_of(".eE") == std::string::npos) {
            const std::size_t fractionStart = _position;
            while (_position < _text.size() && _isDigit(_text[_position])) ++_position;
            if (_position > fractionStart) {
                mantissa += "." + std::string(_text.substr(fractionStart, _position - fractionStart));
            }
        }
        // The unit, if any.
        while (_position < _text.size() && std::isalpha(static_cast<unsigned char>(_text[_position])) != 0) {
            ++_position;
        }

        if (mantissa.empty() || mantissa == ".") return std::nullopt;
        try {
            std::size_t used = 0;
            const double value = std::stod(mantissa, &used);
            if (used != mantissa.size()) return std::nullopt;
            return value * scale;
        } catch (...) {
            return std::nullopt;
        }
    }

    std::optional<double> _identifier() {
        const std::size_t start = _position;
        while (_position < _text.size() && _isIdentifierPart(_text[_position])) ++_position;
        const std::string name = _lower(_text.substr(start, _position - start));

        if (!_consume("(")) {
            const auto found = _parameters.find(name);
            if (found != _parameters.end()) return found->second;
            if (name == "pi") return M_PI;
            return std::nullopt;
        }
        std::vector<double> arguments;
        if (!_consume(")")) {
            do {
                const std::optional<double> argument = _expression();
                if (!argument) return std::nullopt;
                arguments.push_back(*argument);
            } while (_consume(","));
            if (!_consume(")")) return std::nullopt;
        }
        return _call(name, arguments);
    }

    static std::optional<double> _call(const std::string& name, const std::vector<double>& arguments) {
        if (arguments.size() == 1) {
            const double x = arguments[0];
            if (name == "abs") return std::abs(x);
            if (name == "sqrt") return std::sqrt(x);
            if (name == "exp") return std::exp(x);
            if (name == "log" || name == "ln") return std::log(x);
            if (name == "log10") return std::log10(x);
        } else if (arguments.size() == 2) {
            if (name == "pow" || name == "pwr") return std::pow(arguments[0], arguments[1]);
            if (name == "min") return std::min(arguments[0], arguments[1]);
            if (name == "max") return std::max(arguments[0], arguments[1]);
        }
        return std::nullopt;
    }

    std::string_view _text;
    const SpiceParameters& _parameters;
    std::size_t _position = 0;
};

std::string_view _trim(std::string_view text) {
    while (!text.empty() && _isSpace(text.front())) text.remove_prefix(1);
    while (!text.empty() && _isSpace(text.back())) text.remove_suffix(1);
    return text;
}

} // namespace

std::optional<double> evaluateSpiceExpression(std::string_view text, const SpiceParameters& parameters) {
    text = _trim(text);
    // '...' is the quoted form some dialects use instead of {...}; {...} itself parses as grouping.
    if (text.size() >= 2 && text.front() == '\'' && text.back() == '\'') text = text.substr(1, text.size() - 2);
    if (text.empty()) return std::nullopt;
    return Parser(text, parameters).parse();
}

std::optional<double> evaluateElementValue(char kind, std::string_view value, const SpiceParameters& parameters) {
    const char principal = _lower(kind);
    std::optional<std::string_view> found;
    // Tokens are whitespace-separated, except inside {...} or '...'.
    std::size_t position = 0;
    while (position < value.size()) {
        while (position < value.size() && _isSpace(value[position])) ++position;
        if (position >= value.size()) break;
        const std::size_t start = position;
        int depth = 0;
        bool quoted = false;
        while (position < value.size() && (depth > 0 || quoted || !_isSpace(value[position]))) {
            const char c = value[position++];
            if (c == '\'') quoted = !quoted;
            else if (!quoted && c == '{') ++depth;
            else if (!quoted && c == '}') --depth;
        }
        const std::string_view token = value.substr(start, position - start);
        const std::size_t equals = token.find('=');
        if (equals == std::string_view::npos) {
            if (!found) found = token;
            continue;
        }
        const std::string name = _lower(_trim(token.substr(0, equals)));
        if (name.size() == 1 && name[0] == principal) return evaluateSpiceExpression(token.substr(equals + 1), parameters);
    }
    return found ? evaluateSpiceExpression(*found, parameters) : std::nullopt;
}

} // namespace libkicad::detail
