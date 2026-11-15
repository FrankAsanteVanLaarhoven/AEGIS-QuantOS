#include "aq/json.hpp"

#include <cctype>
#include <cmath>
#include <cstdlib>
#include <limits>

namespace aq {

namespace {

class Parser {
public:
    Parser(const std::string& s, int max_depth) : s_(s), max_depth_(max_depth) {}

    JsonValue parse() {
        skip_ws();
        JsonValue v = parse_value(0);
        skip_ws();
        if (pos_ != s_.size()) {
            throw std::runtime_error("Trailing data after JSON value");
        }
        return v;
    }

private:
    const std::string& s_;
    size_t pos_ = 0;
    int max_depth_;

    void skip_ws() {
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n') pos_++;
            else break;
        }
    }

    char peek() const {
        if (pos_ >= s_.size()) throw std::runtime_error("Unexpected end of JSON");
        return s_[pos_];
    }

    char get() {
        if (pos_ >= s_.size()) throw std::runtime_error("Unexpected end of JSON");
        return s_[pos_++];
    }

    JsonValue parse_value(int depth) {
        if (depth > max_depth_) throw std::runtime_error("JSON nesting depth exceeded");
        skip_ws();
        char c = peek();
        if (c == '{') return parse_object(depth + 1);
        if (c == '[') return parse_array(depth + 1);
        if (c == '"') return JsonValue{parse_string()};
        if (c == 't') {
            expect("true");
            return JsonValue{true};
        }
        if (c == 'f') {
            expect("false");
            return JsonValue{false};
        }
        if (c == 'n') {
            expect("null");
            return JsonValue{nullptr};
        }
        if (c == '-' || std::isdigit(static_cast<unsigned char>(c))) {
            return parse_number();
        }
        throw std::runtime_error("Invalid JSON token");
    }

    void expect(const char* lit) {
        while (*lit) {
            if (get() != *lit++) throw std::runtime_error("Invalid JSON literal");
        }
    }

    std::string parse_string() {
        if (get() != '"') throw std::runtime_error("Expected '\"'");
        std::string out;
        while (pos_ < s_.size()) {
            char c = get();
            if (c == '"') return out;
            if (static_cast<unsigned char>(c) < 0x20) {
                throw std::runtime_error("Unescaped control character in JSON string");
            }
            if (c == '\\') {
                char esc = get();
                switch (esc) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'u': {
                        if (pos_ + 4 > s_.size()) throw std::runtime_error("Invalid unicode escape");
                        unsigned int cp = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = get();
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp |= static_cast<unsigned int>(h - '0');
                            else if (h >= 'a' && h <= 'f') cp |= static_cast<unsigned int>(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') cp |= static_cast<unsigned int>(h - 'A' + 10);
                            else throw std::runtime_error("Invalid hex in unicode escape");
                        }
                        if (cp < 0x80) out.push_back(static_cast<char>(cp));
                        else out.push_back('?');
                        break;
                    }
                    default: throw std::runtime_error("Invalid JSON escape");
                }
            } else {
                out.push_back(c);
            }
        }
        throw std::runtime_error("Unterminated JSON string");
    }

    JsonValue parse_number() {
        size_t start = pos_;
        bool is_float = false;
        if (s_[pos_] == '-') pos_++;
        if (pos_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
            throw std::runtime_error("Invalid JSON number");
        }
        if (s_[pos_] == '0') {
            pos_++;
            if (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
                throw std::runtime_error("Leading zero in JSON number");
            }
        } else {
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) pos_++;
        }
        if (pos_ < s_.size() && s_[pos_] == '.') {
            is_float = true;
            pos_++;
            if (pos_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
                throw std::runtime_error("Invalid JSON fraction");
            }
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) pos_++;
        }
        if (pos_ < s_.size() && (s_[pos_] == 'e' || s_[pos_] == 'E')) {
            is_float = true;
            pos_++;
            if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) pos_++;
            if (pos_ >= s_.size() || !std::isdigit(static_cast<unsigned char>(s_[pos_]))) {
                throw std::runtime_error("Invalid JSON exponent");
            }
            while (pos_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[pos_]))) pos_++;
        }
        std::string num_str = s_.substr(start, pos_ - start);
        if (!is_float) {
            char* endp = nullptr;
            errno = 0;
            long long val = std::strtoll(num_str.c_str(), &endp, 10);
            if (errno == 0 && endp == num_str.c_str() + num_str.size()) {
                return JsonValue{static_cast<int64_t>(val)};
            }
        }
        char* endp = nullptr;
        errno = 0;
        double d = std::strtod(num_str.c_str(), &endp);
        if (errno != 0 || endp != num_str.c_str() + num_str.size() || !std::isfinite(d)) {
            throw std::runtime_error("JSON number out of range");
        }
        return JsonValue{d};
    }

    JsonValue parse_array(int depth) {
        get();  // '['
        JsonArray arr;
        skip_ws();
        if (peek() == ']') {
            get();
            return JsonValue{std::move(arr)};
        }
        while (true) {
            arr.push_back(parse_value(depth));
            skip_ws();
            char c = get();
            if (c == ']') break;
            if (c != ',') throw std::runtime_error("Expected ',' or ']' in JSON array");
        }
        return JsonValue{std::move(arr)};
    }

    JsonValue parse_object(int depth) {
        get();  // '{'
        JsonObject obj;
        skip_ws();
        if (peek() == '}') {
            get();
            return JsonValue{std::move(obj)};
        }
        while (true) {
            skip_ws();
            if (peek() != '"') throw std::runtime_error("Expected object key string");
            std::string key = parse_string();
            skip_ws();
            if (get() != ':') throw std::runtime_error("Expected ':' after object key");
            JsonValue val = parse_value(depth);
            auto [it, inserted] = obj.emplace(std::move(key), std::move(val));
            if (!inserted) {
                throw std::runtime_error("Duplicate JSON object key");
            }
            skip_ws();
            char c = get();
            if (c == '}') break;
            if (c != ',') throw std::runtime_error("Expected ',' or '}' in JSON object");
        }
        return JsonValue{std::move(obj)};
    }
};

}  // namespace

JsonValue parse_json_strict(const std::string& input, size_t max_bytes, int max_depth) {
    if (input.size() > max_bytes) {
        throw std::runtime_error("JSON payload exceeds max_bytes");
    }
    Parser p(input, max_depth);
    return p.parse();
}

}  // namespace aq

