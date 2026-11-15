#include "aq/data.hpp"
#include "aq/storage.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <random>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace aq {

namespace {
int64_t days_from_civil(int y, unsigned m, unsigned d) {
    y -= m <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return static_cast<int64_t>(era) * 146097 + static_cast<int64_t>(doe) - 719468;
}

std::string format_iso_utc(int64_t epoch_s) {
    std::time_t tt = static_cast<std::time_t>(epoch_s);
    std::tm tm_utc{};
    gmtime_r(&tt, &tm_utc);
    char buf[80];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ",
                  tm_utc.tm_year + 1900, tm_utc.tm_mon + 1, tm_utc.tm_mday,
                  tm_utc.tm_hour, tm_utc.tm_min, tm_utc.tm_sec);
    return std::string(buf);
}

std::vector<std::string> split_csv_line(const std::string& line) {
    std::vector<std::string> out;
    std::string cur;
    for (char ch : line) {
        if (ch == '\r') continue;
        if (ch == ',') {
            out.push_back(cur);
            cur.clear();
        } else {
            cur.push_back(ch);
        }
    }
    out.push_back(cur);
    return out;
}

double parse_finite_pos(const std::string& s, const char* name) {
    if (s.empty()) throw std::invalid_argument(std::string("Empty ") + name);
    size_t idx = 0;
    double v = std::stod(s, &idx);
    if (idx != s.size() || !std::isfinite(v) || v <= 0.0) {
        throw std::invalid_argument(std::string("Invalid ") + name);
    }
    return v;
}

int64_t parse_int64_nonneg(const std::string& s, const char* name) {
    if (s.empty()) throw std::invalid_argument(std::string("Empty ") + name);
    size_t idx = 0;
    double dv = std::stod(s, &idx);
    if (idx != s.size() || !std::isfinite(dv) || dv < 0.0 || std::floor(dv) != dv) {
        throw std::invalid_argument(std::string("Non-integer ") + name);
    }
    return static_cast<int64_t>(dv);
}
}  // namespace

int64_t parse_iso8601_utc(const std::string& ts) {
    if (ts.size() < 20) throw std::invalid_argument("Timezone-aware ISO-8601 timestamp required");
    int y = 0, m = 0, d = 0, hh = 0, mm = 0, ss = 0;
    if (std::sscanf(ts.c_str(), "%4d-%2d-%2dT%2d:%2d:%2d", &y, &m, &d, &hh, &mm, &ss) != 6) {
        throw std::invalid_argument("Invalid ISO-8601 timestamp: " + ts);
    }
    if (m < 1 || m > 12 || d < 1 || d > 31 || hh < 0 || hh > 23 || mm < 0 || mm > 59 || ss < 0 || ss > 59) {
        throw std::invalid_argument("Out-of-range ISO-8601 fields: " + ts);
    }
    int tz_offset_s = 0;
    size_t tz_pos = ts.find_first_of("Z+-", 19);
    if (tz_pos == std::string::npos) {
        throw std::invalid_argument("Timezone-aware timestamp required: " + ts);
    }
    if (ts[tz_pos] != 'Z') {
        int tzh = 0, tzm = 0;
        char sign = ts[tz_pos];
        if (std::sscanf(ts.c_str() + tz_pos + 1, "%2d:%2d", &tzh, &tzm) != 2) {
            throw std::invalid_argument("Invalid timezone offset: " + ts);
        }
        tz_offset_s = (tzh * 3600 + tzm * 60) * (sign == '+' ? 1 : -1);
    }
    int64_t days = days_from_civil(y, static_cast<unsigned>(m), static_cast<unsigned>(d));
    return days * 86400LL + hh * 3600LL + mm * 60LL + ss - tz_offset_s;
}

Dataset parse_csv_strict(const std::string& csv_text, const std::string& source) {
    if (csv_text.empty() || csv_text.size() > 16000000ULL) {
        throw std::invalid_argument("CSV empty or exceeds size limit");
    }
    std::istringstream iss(csv_text);
    std::string header_line;
    if (!std::getline(iss, header_line)) throw std::invalid_argument("Missing CSV header");
    auto headers = split_csv_line(header_line);
    std::unordered_map<std::string, size_t> col;
    for (size_t i = 0; i < headers.size(); ++i) col[headers[i]] = i;
    for (const char* req : {"symbol", "open_at", "timestamp", "available_at", "open", "high", "low", "close", "volume"}) {
        if (col.find(req) == col.end()) {
            throw std::invalid_argument(std::string("Missing required CSV column: ") + req);
        }
    }

    Dataset ds;
    ds.source = source;
    std::string line;
    while (std::getline(iss, line)) {
        if (line.empty() || line == "\r") continue;
        auto fields = split_csv_line(line);
        if (fields.size() < headers.size()) throw std::invalid_argument("Truncated CSV row");
        if (col.count("split_factor")) {
            double sf = std::stod(fields[col["split_factor"]]);
            if (sf != 1.0) throw std::invalid_argument("Corporate actions not supported (split_factor != 1)");
        }
        if (col.count("dividend")) {
            double dv = std::stod(fields[col["dividend"]]);
            if (dv != 0.0) throw std::invalid_argument("Corporate actions not supported (dividend != 0)");
        }
        BarRow r;
        r.symbol = fields[col["symbol"]];
        if (r.symbol.empty() || r.symbol.size() > 12) throw std::invalid_argument("Invalid symbol");
        if (ds.symbol.empty()) ds.symbol = r.symbol;
        else if (ds.symbol != r.symbol) throw std::invalid_argument("Single-asset dataset required in v1");

        r.open_at = fields[col["open_at"]];
        r.timestamp = fields[col["timestamp"]];
        r.available_at = fields[col["available_at"]];
        r.open_epoch_s = parse_iso8601_utc(r.open_at);
        r.close_epoch_s = parse_iso8601_utc(r.timestamp);
        r.available_epoch_s = parse_iso8601_utc(r.available_at);
        if (r.open_epoch_s >= r.close_epoch_s || r.available_epoch_s < r.close_epoch_s) {
            throw std::invalid_argument("Invalid availability chronology");
        }
        r.open = parse_finite_pos(fields[col["open"]], "open");
        r.high = parse_finite_pos(fields[col["high"]], "high");
        r.low = parse_finite_pos(fields[col["low"]], "low");
        r.close = parse_finite_pos(fields[col["close"]], "close");
        r.volume = parse_int64_nonneg(fields[col["volume"]], "volume");
        if (r.high < std::max({r.open, r.close, r.low}) || r.low > std::min({r.open, r.close, r.high})) {
            throw std::invalid_argument("Invalid OHLC envelope");
        }
        r.open_cents = static_cast<int64_t>(std::llround(r.open * 100.0));
        r.close_cents = static_cast<int64_t>(std::llround(r.close * 100.0));
        if (r.open_cents <= 0 || r.close_cents <= 0) throw std::invalid_argument("Non-positive cents price");
        ds.bars.push_back(std::move(r));
    }
    if (ds.bars.size() < 65) throw std::invalid_argument("At least 65 bars required");
    std::sort(ds.bars.begin(), ds.bars.end(), [](const BarRow& a, const BarRow& b) {
        return a.close_epoch_s < b.close_epoch_s;
    });
    for (size_t i = 1; i < ds.bars.size(); ++i) {
        if (ds.bars[i].close_epoch_s == ds.bars[i - 1].close_epoch_s) {
            throw std::invalid_argument("Duplicate timestamp");
        }
        if (ds.bars[i].open_epoch_s < ds.bars[i - 1].available_epoch_s) {
            throw std::invalid_argument("Next open must not precede prior availability");
        }
    }
    ds.dataset_hash = sha256_hex(to_csv(ds));
    return ds;
}

Dataset load_csv_file(const std::string& path, const std::string& source) {
    std::ifstream ifs(path, std::ios::binary);
    if (!ifs) throw std::runtime_error("Cannot open CSV file: " + path);
    std::ostringstream oss;
    oss << ifs.rdbuf();
    return parse_csv_strict(oss.str(), source);
}

Dataset generate_synthetic_dataset(size_t count, uint64_t seed, const std::string& symbol) {
    if (count < 65) throw std::invalid_argument("At least 65 bars required");
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> gauss(0.0, 0.0018);
    std::uniform_int_distribution<int64_t> vol_dist(15000, 120000);

    Dataset ds;
    ds.symbol = symbol;
    ds.source = "SYNTHETIC";
    int64_t t0 = parse_iso8601_utc("2026-11-02T14:30:00Z");
    double px = 128.00;
    for (size_t i = 0; i < count; ++i) {
        int64_t open_t = t0 + static_cast<int64_t>(i) * 300;
        int64_t close_t = open_t + 299;
        int64_t avail_t = open_t + 300;
        double op = std::max(1.0, std::round(px * (1.0 + 0.25 * gauss(rng)) * 100.0) / 100.0);
        double ret = gauss(rng) + 0.00015 * std::sin(static_cast<double>(i) / 19.0);
        double cl = std::max(1.0, std::round(op * (1.0 + ret) * 100.0) / 100.0);
        double spread = std::max(0.01, std::round(std::max(op, cl) * 0.0012 * 100.0) / 100.0);
        double hi = std::round((std::max(op, cl) + spread) * 100.0) / 100.0;
        double lo = std::max(0.5, std::round((std::min(op, cl) - spread) * 100.0) / 100.0);
        BarRow r;
        r.symbol = symbol;
        r.open_at = format_iso_utc(open_t);
        r.timestamp = format_iso_utc(close_t);
        r.available_at = format_iso_utc(avail_t);
        r.open = op;
        r.high = hi;
        r.low = lo;
        r.close = cl;
        r.volume = vol_dist(rng);
        r.open_cents = static_cast<int64_t>(std::llround(op * 100.0));
        r.close_cents = static_cast<int64_t>(std::llround(cl * 100.0));
        r.open_epoch_s = open_t;
        r.close_epoch_s = close_t;
        r.available_epoch_s = avail_t;
        ds.bars.push_back(std::move(r));
        px = cl;
    }
    ds.dataset_hash = sha256_hex(to_csv(ds));
    return ds;
}

std::string to_csv(const Dataset& ds) {
    std::ostringstream oss;
    oss << "symbol,open_at,timestamp,available_at,open,high,low,close,volume\n";
    oss << std::fixed << std::setprecision(2);
    for (const auto& b : ds.bars) {
        oss << b.symbol << "," << b.open_at << "," << b.timestamp << "," << b.available_at << ","
            << b.open << "," << b.high << "," << b.low << "," << b.close << "," << b.volume << "\n";
    }
    return oss.str();
}

void write_csv_file(const std::string& path, const Dataset& ds) {
    std::ofstream ofs(path, std::ios::binary);
    if (!ofs) throw std::runtime_error("Cannot write CSV file: " + path);
    ofs << to_csv(ds);
}

}  // namespace aq
