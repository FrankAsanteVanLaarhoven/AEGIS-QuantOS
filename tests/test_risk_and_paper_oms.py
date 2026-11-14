from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
import httpx
import pytest

from aegis.brokers import AlpacaPaperOMS
from aegis.risk import DEFAULT_PROFILES, IndependentRiskGate, OrderIntent
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings


def test_1000_prohibited_order_fault_cases():
    gate = IndependentRiskGate(DEFAULT_PROFILES['quantshield_ftmo_style_v1'])
    rng = random.Random(42)
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    accepted_prohibited = 0

    for i in range(1050):
        fault_kind = i % 10
        expires = now + timedelta(seconds=60)
        data_age = 1.0
        halted = False
        integrity_ok = True
        side = 'BUY'
        qty = 10.0
        price = 100.0
        cash = 50000.0
        equity = 100000.0
        positions = {'NVDA': 0.0}
        daily_pnl_pct = 0.0
        total_dd_pct = 0.0

        if fault_kind == 0:
            expires = now - timedelta(seconds=1)  # expired
        elif fault_kind == 1:
            data_age = 120.0  # stale
        elif fault_kind == 2:
            halted = True  # emergency halt
        elif fault_kind == 3:
            integrity_ok = False  # ledger/reconciliation failure
        elif fault_kind == 4:
            side = 'SELL'
            qty = float(rng.randint(1, 50))  # short sell (0 held)
        elif fault_kind == 5:
            cash = 100.0  # insufficient cash
        elif fault_kind == 6:
            daily_pnl_pct = -0.06  # breaches 5% daily loss
        elif fault_kind == 7:
            total_dd_pct = 0.12  # breaches 10% total drawdown
        elif fault_kind == 8:
            qty = 300.0  # 30k notional > 20% of 100k
        elif fault_kind == 9:
            positions = {'NVDA': 250.0}
            qty = 100.0  # concentration > 30%

        intent = OrderIntent(
            symbol='NVDA',
            side=side,  # type: ignore[arg-type]
            qty=qty,
            client_order_id=f'fault_{i}',
            expires_at=expires,
            data_age_seconds=data_age,
        )
        dec = gate.evaluate(
            intent,
            price=price,
            equity=equity,
            cash=cash,
            positions=positions,
            daily_pnl_pct=daily_pnl_pct,
            total_drawdown_pct=total_dd_pct,
            halted=halted,
            integrity_ok=integrity_ok,
            now=now,
        )
        if dec.approved:
            accepted_prohibited += 1

    assert accepted_prohibited == 0


def test_risk_reducing_sell_allowed_during_halt_and_cross_symbol_reservation():
    gate = IndependentRiskGate(DEFAULT_PROFILES['generic_paper_v1'])
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    sell_intent = OrderIntent(
        symbol='NVDA', side='SELL', qty=10, client_order_id='reduce_1',
        expires_at=now + timedelta(seconds=60), data_age_seconds=999.0,
    )
    dec = gate.evaluate(sell_intent, price=100.0, equity=100000.0, cash=50000.0, positions={'NVDA': 20.0}, halted=True, integrity_ok=False, now=now)
    assert dec.approved is True and dec.risk_reducing is True


def test_external_paper_oms_timeout_reconcile_and_idempotency():
    settings = AegisSettings.from_env({'AEGIS_EXECUTION_MODE': 'paper_external'})
    resolver = SecretResolver({'ALPACA_API_KEY': 'test_alp_key_1234', 'ALPACA_SECRET_KEY': 'test_alp_sec_1234'})
    gate = IndependentRiskGate()
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    intent = OrderIntent(
        environment='paper_external', symbol='NVDA', side='BUY', qty=5,
        client_order_id='ext_cid_1', expires_at=now + timedelta(seconds=60), data_age_seconds=1.0,
    )
    dec = gate.evaluate(intent, price=100.0, equity=100000.0, cash=50000.0, positions={}, now=now)
    assert dec.approved

    calls = {'post': 0, 'get': 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == 'POST':
            calls['post'] += 1
            raise httpx.ReadTimeout('network timeout')
        calls['get'] += 1
        return httpx.Response(200, json={'id': 'brk_999', 'status': 'filled', 'filled_qty': '5', 'filled_avg_price': '100.5'})

    oms = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(handler)))
    rec = oms.submit_order(intent, dec)
    assert rec.state == 'UNKNOWN'
    assert calls['post'] == 1

    # Duplicate submit with same client_order_id must be idempotent (no second POST)
    rec_dup = oms.submit_order(intent, dec)
    assert rec_dup.state == 'UNKNOWN'
    assert calls['post'] == 1

    # Reconcile by client_order_id resolves UNKNOWN -> FILLED
    rec_final = oms.reconcile_by_client_id('ext_cid_1')
    assert rec_final.state == 'FILLED'
    assert rec_final.filled_qty == 5.0
    assert rec_final.filled_avg_price == 100.5


def test_c1_finite_validation_marks_and_pending_reservations(tmp_path):
    from aegis.store import Store

    store = Store(tmp_path / 'risk.sqlite')
    gate = IndependentRiskGate(store=store)
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)

    # 1. NaN / Inf price, equity, cash rejected
    for bad_kw in [
        {'price': float('nan'), 'equity': 1000.0, 'cash': 1000.0},
        {'price': 10.0, 'equity': float('inf'), 'cash': 1000.0},
        {'price': 10.0, 'equity': 1000.0, 'cash': float('nan')},
    ]:
        it = OrderIntent(
            symbol='AAA', side='BUY', qty=1, client_order_id='bad_' + str(bad_kw),
            expires_at=now + timedelta(minutes=5), data_age_seconds=0.0,
        )
        assert not gate.evaluate(it, positions={}, now=now, **bad_kw).approved

    # 2. Missing per-instrument mark fails closed; valid marks below gross cap pass
    other_it = OrderIntent(
        symbol='AAA', side='BUY', qty=1, client_order_id='marks_1',
        expires_at=now + timedelta(minutes=5), data_age_seconds=0.0,
    )
    dec_missing = gate.evaluate(other_it, price=1.0, equity=10000.0, cash=1000.0, positions={'BBB': 10.0}, now=now)
    assert not dec_missing.approved
    assert 'MISSING_POSITION_MARKS' in dec_missing.reasons

    dec_ok = gate.evaluate(
        other_it, price=1.0, equity=10000.0, cash=1000.0,
        positions={'BBB': 10.0}, marks={'BBB': 100.0}, now=now,
    )
    assert dec_ok.approved

    # 3. Pending BUY cash reservation persists across gate restart
    gate_restarted = IndependentRiskGate(store=store)
    buy2 = OrderIntent(
        symbol='CCC', side='BUY', qty=100, client_order_id='buy_2',
        expires_at=now + timedelta(minutes=5), data_age_seconds=0.0,
    )
    # 100 * 10 = 1000 > available cash (1000 - 1 = 999)
    dec_cash = gate_restarted.evaluate(
        buy2, price=10.0, equity=10000.0, cash=1000.0,
        positions={'BBB': 10.0}, marks={'BBB': 100.0}, now=now,
    )
    assert not dec_cash.approved
    assert 'INSUFFICIENT_CASH' in dec_cash.reasons

    # 4. Pending SELL reservations prevent overselling across two orders and survive restart
    sell1 = OrderIntent(
        symbol='BBB', side='SELL', qty=7, client_order_id='sell_1',
        expires_at=now + timedelta(minutes=5), data_age_seconds=0.0,
    )
    sell2 = OrderIntent(
        symbol='BBB', side='SELL', qty=7, client_order_id='sell_2',
        expires_at=now + timedelta(minutes=5), data_age_seconds=0.0,
    )
    assert gate_restarted.evaluate(sell1, price=100.0, equity=10000.0, cash=1000.0, positions={'BBB': 10.0}, now=now).approved
    gate_restarted2 = IndependentRiskGate(store=store)
    dec_sell2 = gate_restarted2.evaluate(sell2, price=100.0, equity=10000.0, cash=1000.0, positions={'BBB': 10.0}, now=now)
    assert not dec_sell2.approved
    assert 'SHORT_SELLING_PROHIBITED' in dec_sell2.reasons


