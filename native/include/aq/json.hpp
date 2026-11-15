#pragma once

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace aq {

struct JsonValue;
using JsonArray = std::vector<JsonValue>;
using JsonObject = std::map<std::string, JsonValue>;

struct JsonValue {
    using Variant = std::variant<std::nullptr_t, bool, int64_t, double, std::string, JsonArray, JsonObject>;
    Variant data;

    bool is_null() const { return std::holds_alternative<std::nullptr_t>(data); }
    bool is_bool() const { return std::holds_alternative<bool>(data); }
    bool is_int64() const { return std::holds_alternative<int64_t>(data); }
    bool is_double() const { return std::holds_alternative<double>(data); }
    bool is_string() const { return std::holds_alternative<std::string>(data); }
    bool is_array() const { return std::holds_alternative<JsonArray>(data); }
    bool is_object() const { return std::holds_alternative<JsonObject>(data); }

    int64_t as_int64() const {
        if (!is_int64()) throw std::runtime_error("Expected exact integer JSON value");
        return std::get<int64_t>(data);
    }
    const std::string& as_string() const {
        if (!is_string()) throw std::runtime_error("Expected string JSON value");
        return std::get<std::string>(data);
    }
    const JsonObject& as_object() const {
        if (!is_object()) throw std::runtime_error("Expected object JSON value");
        return std::get<JsonObject>(data);
    }
};

// Strict bounded RFC 8259 parser: rejects duplicate keys, trailing tokens, and excessive depth/size.
JsonValue parse_json_strict(const std::string& input, size_t max_bytes = 1048576, int max_depth = 32);

// Explicit identifier validation: [A-Za-z0-9._:-], bounded length, no NUL/whitespace/control chars.
bool is_valid_identifier(const std::string& s, size_t max_len = 128);
bool has_embedded_nul(const std::string& s);

}  // namespace aq

