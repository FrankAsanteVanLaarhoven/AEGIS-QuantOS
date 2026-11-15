#include "aq/data.hpp"
#include "aq/rl.hpp"
#include "aq/service.hpp"

#include <atomic>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

namespace {
std::atomic<bool> g_stop_serve{false};
void on_sig(int) { g_stop_serve.store(true); }
}

int main(int argc, char** argv) {
    try {
        if (argc < 2) {
            std::cerr << "Usage: aegis_native <fixture|train|infer|replay|shadow|serve|backup|rollback> ...\n";
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
        if (cmd == "shadow") {
            if (argc < 4) return 2;
            std::string ckpt_hash;
            auto model = aq::ActorCritic::load_checkpoint(argv[2], &ckpt_hash);
            auto ds = aq::load_csv_file(argv[3], "SYNTHETIC");
            auto rep = aq::run_replay_repeatability_check(ds, aq::default_sim_config(), &model, ckpt_hash);
            std::vector<uint32_t> tape(ds.bars.size() - 17, 1);
            auto par = aq::run_independent_shadow_parity(ds, aq::default_sim_config(), tape);
            std::cout << "{\"command\":\"shadow\",\"exact_repeatability\":" << (rep.exact_repeatability ? "true" : "false")
                      << ",\"exact_per_event_parity\":" << (par.exact_per_event_parity ? "true" : "false")
                      << ",\"events_compared\":" << par.events_compared
                      << ",\"native_final_equity_cents\":" << par.native_final_equity_cents
                      << ",\"receipt_hash\":\"" << rep.receipt_hash << "\"}\n";
            return (rep.exact_repeatability && par.exact_per_event_parity) ? 0 : 1;
        }
        if (cmd == "serve") {
            if (argc < 5) return 2;
            aq::ServiceConfig scfg;
            scfg.db_path = argv[2];
            scfg.host = argv[3];
            scfg.port = static_cast<uint16_t>(std::stoul(argv[4]));
            const char* tok_env = std::getenv("AEGIS_API_TOKEN");
            scfg.auth_token = (argc >= 6) ? argv[5] : (tok_env ? tok_env : "dev-native-token");
            aq::NativeHttpServer srv(scfg);
            srv.load_dataset(aq::generate_synthetic_dataset(240, 26));
            srv.start();
            std::signal(SIGINT, on_sig);
            std::signal(SIGTERM, on_sig);
            std::cout << "{\"command\":\"serve\",\"host\":\"" << scfg.host
                      << "\",\"port\":" << srv.bound_port() << ",\"live_execution\":false}\n"
                      << std::flush;
            while (!g_stop_serve.load()) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            srv.stop();
            return 0;
        }
        if (cmd == "backup") {
            if (argc < 5) return 2;
            aq::NativeOms oms(argv[2]);
            auto mf = oms.create_backup_manifest(argv[3], argv[4]);
            std::cout << "{\"command\":\"backup\",\"schema_version\":" << mf.schema_version
                      << ",\"code_sha\":\"" << mf.code_sha
                      << "\",\"model_format\":\"" << mf.model_format
                      << "\",\"backup_hash\":\"" << mf.backup_hash
                      << "\",\"manifest_path\":\"" << mf.manifest_path << "\"}\n";
            return 0;
        }
        if (cmd == "rollback") {
            if (argc < 4) return 2;
            aq::NativeOms::restore_from_backup_manifest(argv[2], argv[3]);
            std::cout << "{\"command\":\"rollback\",\"manifest_path\":\"" << argv[2]
                      << "\",\"target_db\":\"" << argv[3] << "\",\"restored\":true}\n";
            return 0;
        }
        return 2;
    } catch (const std::exception& exc) {
        std::cerr << "Error: " << exc.what() << "\n";
        return 1;
    }
}

