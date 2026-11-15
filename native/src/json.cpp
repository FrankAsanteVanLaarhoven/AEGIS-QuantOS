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

    static uint32_t parse_hex4(const std::string& s, size_t& pos) {
        if (pos + 4 > s.size()) throw std::runtime_error("Invalid unicode escape");
        uint32_t cp = 0;
        for (int i = 0; i < 4; ++i) {
            char h = s[pos++];
            cp <<= 4;
            if (h >= '0' && h <= '9') cp |= static_cast<uint32_t>(h - '0');
            else if (h >= 'a' && h <= 'f') cp |= static_cast<uint32_t>(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') cp |= static_cast<uint32_t>(h - 'A' + 10);
            else throw std::runtime_error("Invalid hex in unicode escape");
        }
        return cp;
    }

    static void append_utf8(std::string& out, uint32_t cp) {
        if (cp <= 0x7Fu) {
            out.push_back(static_cast<char>(cp));
        } else if (cp <= 0x7FFu) {
            out.push_back(static_cast<char>(0xC0u | (cp >> 6)));
            out.push_back(static_cast<char>(0x80u | (cp & 0x3Fu)));
        } else if (cp <= 0xFFFFu) {
            if (cp >= 0xD800u && cp <= 0xDFFFu) {
                throw std::runtime_error("Invalid surrogate code point");
            }
            out.push_back(static_cast<char>(0xE0u | (cp >> 12)));
            out.push_back(static_cast<char>(0x80u | ((cp >> 6) & 0x3Fu)));
            out.push_back(static_cast<char>(0x80u | (cp & 0x3Fu)));
        } else if (cp <= 0x10FFFFu) {
            out.push_back(static_cast<char>(0xF0u | (cp >> 18)));
            out.push_back(static_cast<char>(0x80u | ((cp >> 12) & 0x3Fu)));
            out.push_back(static_cast<char>(0x80u | ((cp >> 6) & 0x3Fu)));
            out.push_back(static_cast<char>(0x80u | (cp & 0x3Fu)));
        } else {
            throw std::runtime_error("Unicode code point out of range");
        }
    }

    std::string parse_string() {
        if (get() != '"') throw std::runtime_error("Expected '\"'");
        std::string out;
        while (pos_ < s_.size()) {
            unsigned char uc = static_cast<unsigned char>(get());
            if (uc == '"') return out;
            if (uc < 0x20u) {
                throw std::runtime_error("Unescaped control character in JSON string");
            }
            if (uc == '\\') {
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
                        uint32_t u1 = parse_hex4(s_, pos_);
                        if (u1 >= 0xDC00u && u1 <= 0xDFFFu) {
                            throw std::runtime_error("Lone low surrogate in JSON string");
                        }
                        if (u1 >= 0xD800u && u1 <= 0xDBFFu) {
                            if (pos_ + 2 > s_.size() || s_[pos_] != '\\' || s_[pos_ + 1] != 'u') {
                                throw std::runtime_error("Unpaired high surrogate in JSON string");
                            }
                            pos_ += 2;
                            uint32_t u2 = parse_hex4(s_, pos_);
                            if (u2 < 0xDC00u || u2 > 0xDFFFu) {
                                throw std::runtime_error("Invalid low surrogate in JSON string");
                            }
                            uint32_t cp = 0x10000u + (((u1 - 0xD800u) << 10) | (u2 - 0xDC00u));
                            append_utf8(out, cp);
                        } else {
                            append_utf8(out, u1);
                        }
                        break;
                    }
                    default: throw std::runtime_error("Invalid JSON escape");
                }
            } else if (uc < 0x80u) {
                out.push_back(static_cast<char>(uc));
            } else {
                // Validate raw UTF-8 multibyte sequence strictly
                size_t len = 0;
                uint32_t cp = 0;
                uint32_t min_cp = 0;
                if ((uc & 0xE0u) == 0xC0u) {
                    len = 2; cp = uc & 0x1Fu; min_cp = 0x80u;
                } else if ((uc & 0xF0u) == 0xE0u) {
                    len = 3; cp = uc & 0x0Fu; min_cp = 0x800u;
                } else if ((uc & 0xF8u) == 0xF0u) {
                    len = 4; cp = uc & 0x07u; min_cp = 0x10000u;
                } else {
                    throw std::runtime_error("Invalid UTF-8 lead byte in JSON string");
                }
                if (pos_ + (len - 1) > s_.size()) {
                    throw std::runtime_error("Truncated UTF-8 sequence in JSON string");
                }
                out.push_back(static_cast<char>(uc));
                for (size_t i = 1; i < len; ++i) {
                    unsigned char cont = static_cast<unsigned char>(s_[pos_++]);
                    if ((cont & 0xC0u) != 0x80u) {
                        throw std::runtime_error("Invalid UTF-8 continuation byte in JSON string");
                    }
                    cp = (cp << 6) | (cont & 0x3Fu);
                    out.push_back(static_cast<char>(cont));
                }
                if (cp < min_cp || cp > 0x10FFFFu || (cp >= 0xD800u && cp <= 0xDFFFu)) {
                    throw std::runtime_error("Invalid or overlong UTF-8 code point in JSON string");
                }
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

bool has_embedded_nul(const std::string& s) {
    return s.find('\0') != std::string::npos;
}

bool is_valid_identifier(const std::string& s, size_t max_len) {
    if (s.empty() || s.size() > max_len || has_embedded_nul(s)) return false;
    for (unsigned char c : s) {
        bool ok = (c >= 'a' && c <= 'z') ||
                  (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') ||
                  c == '.' || c == '_' || c == '-' || c == ':';
        if (!ok) return false;
    }
    return true;
}

}  // namespace aq

