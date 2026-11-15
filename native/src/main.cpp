#include "aq/data.hpp"
#include "aq/rl.hpp"

#include <cstdlib>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    try {
        if (argc < 2) {
            std::cerr << "Usage: aegis_native <fixture|train|infer|replay> ...\n";
            return 2;
        }
        std::string cmd = argv[1];
        if (cmd == "fixture") {
            if (argc < 5) return 2;
            std::string out_csv = argv[2];
            size_t count = static_cast<size_t>(std::stoull(argv[3]));
            uint64_t seed = std::stoull(argv[4]);
            auto ds = aq::generate_synthetic_dataset(count, seed);
            aq::write_csv_file(out_csv, ds);
            std::cout << "{\"command\":\"fixture\",\"bars\":" << ds.bars.size()
                      << ",\"dataset_hash\":\"" << ds.dataset_hash << "\"}\n";
            return 0;
        }
        if (cmd == "train") {
            if (argc < 6) return 2;
            auto ds = aq::load_csv_file(argv[2], "SYNTHETIC");
            std::string out_dir = argv[3];
            aq::TrainConfig cfg;
            cfg.seed = std::stoull(argv[4]);
            cfg.updates = static_cast<uint32_t>(std::stoul(argv[5]));
            auto sum = aq::train_ppo(ds, out_dir, cfg);
            std::cout << "{\"command\":\"train\",\"updates\":" << sum.updates_completed
                      << ",\"transitions\":" << sum.transitions
                      << ",\"eval_equity_usd\":" << sum.final_eval_equity_usd
                      << ",\"checkpoint_hash\":\"" << sum.checkpoint_hash << "\"}\n";
            return 0;
        }
        if (cmd == "infer" || cmd == "replay") {
            if (argc < 4) return 2;
            std::string ckpt_hash;
            auto model = aq::ActorCritic::load_checkpoint(argv[2], &ckpt_hash);
            auto ds = aq::load_csv_file(argv[3], "SYNTHETIC");
            auto rec = aq::run_replay_episode(ds, &model, ckpt_hash);
            std::cout << "{\"command\":\"" << cmd << "\",\"dataset_hash\":\"" << rec.dataset_hash
                      << "\",\"model_hash\":\"" << rec.model_hash
                      << "\",\"receipt_hash\":\"" << rec.receipt_hash
                      << "\",\"final_cash_cents\":" << rec.final_cash_cents
                      << ",\"final_shares\":" << rec.final_shares
                      << ",\"final_equity_cents\":" << rec.final_equity_cents
                      << ",\"steps\":" << rec.steps
                      << ",\"risk_interventions\":" << rec.risk_interventions
                      << ",\"trades_executed\":" << rec.trades_executed
                      << ",\"execution_authorized\":0}\n";
            return 0;
        }
        return 2;
    } catch (const std::exception& exc) {
        std::cerr << "Error: " << exc.what() << "\n";
        return 1;
    }
}

