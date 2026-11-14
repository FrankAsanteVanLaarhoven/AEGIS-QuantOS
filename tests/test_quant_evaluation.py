from __future__ import annotations

import pytest

from aegis.data import synthetic
from aegis.quant import valuation
from aegis.quant_eval import benjamini_hochberg, chronological_splits, dcf_oracle, evaluate_candidates


def test_chronological_splits_enforce_purge_and_no_overlap():
    splits = chronological_splits(360, train_frac=0.5, val_frac=0.25, purge_bars=6)
    tr_s, tr_e = splits['train']
    va_s, va_e = splits['validation']
    te_s, te_e = splits['holdout_test']
    assert tr_e + 6 <= va_s
    assert va_e + 6 <= te_s
    assert te_e == 360


def test_dcf_numerical_oracle_matches_quant_valuation():
    res = valuation(fcf=120.0, growth=0.08, discount=0.11, terminal_growth=0.03, years=5, shares=10.0, debt=50.0, cash=20.0)
    oracle = dcf_oracle(120.0, 0.08, 0.11, 0.03, 5, 10.0, debt=50.0, cash=20.0)
    assert res['per_share'] == pytest.approx(oracle, rel=1e-4, abs=1e-2)


def test_evaluate_candidates_cost_sensitivity_fdr_and_no_trade_negative_control():
    ds = synthetic()
    report = evaluate_candidates(ds, cost_bps_grid=[0.0, 6.0, 20.0], purge_bars=4, min_sharpe_gate=10.0)
    assert report['trials_count'] == 9
    # With impossibly high Sharpe gate (10.0), negative control MUST select cash_no_trade
    assert report['no_trade_fallback_used'] is True
    assert report['selected_candidate']['strategy'] == 'cash_no_trade'

    # Benjamini-Hochberg controls false discoveries
    assert benjamini_hochberg([0.001, 0.04, 0.50], alpha=0.05) == [True, False, False]

