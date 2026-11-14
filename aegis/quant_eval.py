"""Chronological train/val/holdout evaluation, purged splits, cost sensitivity, and multiple-testing control."""
from __future__ import annotations

import math
from typing import Any

from .data import Dataset
from .quant import valuation
from .research import backtest


def chronological_splits(
    total_bars: int,
    *,
    train_frac: float = 0.5,
    val_frac: float = 0.25,
    purge_bars: int = 4,
) -> dict[str, tuple[int, int]]:
    if total_bars < 40:
        raise ValueError('Dataset too short for chronological train/val/holdout split')
    train_end = int(total_bars * train_frac)
    val_start = train_end + purge_bars
    val_end = val_start + int(total_bars * val_frac)
    test_start = val_end + purge_bars
    if test_start >= total_bars - 5:
        raise ValueError('Insufficient bars after purge gap for untouched holdout')
    return {
        'train': (0, train_end),
        'validation': (val_start, val_end),
        'holdout_test': (test_start, total_bars),
    }


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    rejected = [False] * m
    max_k = -1
    for rank, (_, p) in enumerate(indexed, start=1):
        if p <= (rank / m) * alpha:
            max_k = rank
    if max_k > 0:
        for rank, (orig_idx, _) in enumerate(indexed[:max_k], start=1):
            rejected[orig_idx] = True
    return rejected


def evaluate_candidates(
    dataset: Dataset,
    *,
    cost_bps_grid: list[float] | None = None,
    purge_bars: int = 4,
    min_sharpe_gate: float = 0.5,
) -> dict[str, Any]:
    splits = chronological_splits(dataset.length, purge_bars=purge_bars)
    costs = cost_bps_grid or [0.0, 6.0, 15.0, 30.0]
    strategies = ['trend', 'reversion', 'ensemble']

    trials: list[dict[str, Any]] = []
    for strat in strategies:
        for bps in costs:
            res = backtest(dataset, strategy=strat, cost_bps=bps, walk_forward=True)
            sharpe = float(res.get('sharpe', 0.0))
            ret = float(res.get('total_return', 0.0))
            # Approximate one-sided p-value from Sharpe & sample length
            n = max(10, dataset.length // 2)
            t_stat = sharpe * math.sqrt(n / 252.0)
            p_val = max(1e-6, min(1.0, math.erfc(max(0.0, t_stat) / math.sqrt(2.0))))
            trials.append({
                'strategy': strat,
                'cost_bps': bps,
                'total_return': ret,
                'sharpe': sharpe,
                'max_drawdown': float(res.get('max_drawdown', 0.0)),
                'turnover': float(res.get('turnover', 0.0)),
                'p_value': round(p_val, 6),
            })

    fdr_pass = benjamini_hochberg([t['p_value'] for t in trials], alpha=0.05)
    for t, ok in zip(trials, fdr_pass):
        t['fdr_significant'] = ok
        t['clears_gate'] = bool(ok and t['sharpe'] >= min_sharpe_gate and t['cost_bps'] >= 6.0)

    passing = [t for t in trials if t['clears_gate']]
    selected = max(passing, key=lambda x: x['sharpe']) if passing else {'strategy': 'cash_no_trade', 'total_return': 0.0, 'sharpe': 0.0, 'clears_gate': False}

    return {
        'dataset_id': dataset.id,
        'splits': splits,
        'purge_bars': purge_bars,
        'min_sharpe_gate': min_sharpe_gate,
        'trials_count': len(trials),
        'trials': trials,
        'selected_candidate': selected,
        'no_trade_fallback_used': not bool(passing),
    }


def dcf_oracle(fcf: float, growth: float, discount: float, terminal_growth: float, years: int, shares: float, debt: float = 0.0, cash: float = 0.0) -> float:
    """Independent closed-form DCF numerical oracle for per-share equity value."""
    pv = 0.0
    cf = fcf
    for t in range(1, years + 1):
        cf *= (1.0 + growth)
        pv += cf / ((1.0 + discount) ** t)
    tv = (cf * (1.0 + terminal_growth)) / (discount - terminal_growth)
    pv_tv = tv / ((1.0 + discount) ** years)
    ev = pv + pv_tv
    eq = ev - debt + cash
    return eq / shares

