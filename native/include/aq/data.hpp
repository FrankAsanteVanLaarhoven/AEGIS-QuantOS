#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace aq {

struct BarRow {
    std::string symbol;
    std::string open_at;
    std::string timestamp;
    std::string available_at;
    double open = 0.0;
    double high = 0.0;
    double low = 0.0;
    double close = 0.0;
    int64_t volume = 0;
    int64_t open_cents = 0;
    int64_t close_cents = 0;
    int64_t open_epoch_s = 0;
    int64_t close_epoch_s = 0;
    int64_t available_epoch_s = 0;
};

struct Dataset {
    std::string symbol;
    std::string source;
    std::string dataset_hash;
    std::vector<BarRow> bars;
};

int64_t parse_iso8601_utc(const std::string& ts);
Dataset parse_csv_strict(const std::string& csv_text, const std::string& source = "USER_UPLOAD");
Dataset load_csv_file(const std::string& path, const std::string& source = "USER_UPLOAD");
Dataset generate_synthetic_dataset(size_t count, uint64_t seed, const std::string& symbol = "NVDA");
std::string to_csv(const Dataset& ds);
void write_csv_file(const std::string& path, const Dataset& ds);

}  // namespace aq
