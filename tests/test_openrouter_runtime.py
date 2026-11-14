from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx
import pytest

from aegis.llm import (
    BudgetExceeded,
    BudgetLedger,
    OpenRouterError,
    OpenRouterGateway,
    ResearchAssessment,
    validate_tool_allowlist,
)
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings


def test_budget_ledger_concurrent_race_and_unknown_outcome():
    ledger = BudgetLedger(job_budget_usd=0.05, daily_budget_usd=0.05, project_budget_usd=0.05)

    def try_reserve(_):
        try:
            ledger.reserve('job1', 0.02)
            return True
        except BudgetExceeded:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_reserve, range(10)))
    # At most 2 * 0.02 = 0.04 <= 0.05 can succeed
    assert sum(results) == 2
    assert ledger.snapshot()['project_spent_usd'] <= 0.05

    # Unknown outcome must not zero out cost
    ledger2 = BudgetLedger(job_budget_usd=0.10, daily_budget_usd=0.10, project_budget_usd=0.10)
    res = ledger2.reserve('job2', 0.03)
    settled = ledger2.reconcile(res.reservation_id, actual_usd=None, unknown_outcome=True)
    assert settled.status == 'UNKNOWN_OUTCOME'
    assert ledger2.snapshot()['project_spent_usd'] == 0.03


def test_forbidden_tools_and_citation_validation():
    with pytest.raises(ValueError, match='Forbidden LLM tools'):
        validate_tool_allowlist(['sec_lookup', 'broker_submit'])

    ra = ResearchAssessment(
        job_id='j1',
        role='filing_extractor',
        model_id='provider/model-a',
        input_evidence_ids=['doc_1'],
        as_of=datetime.now(timezone.utc),
        extracted_claims=['Revenue grew 10%'],
        cited_evidence_ids=['doc_fabricated'],
    )
    assert ra.validate_citations() is False


def _make_gateway(tmp_path: Path, handler, *, paid: bool = True, key: str | None = 'sk-or-v1-testkey123456789'):
    lock_file = tmp_path / 'lock.json'
    lock_file.write_text(json.dumps({
        'schema_version': 1,
        'roles': {'filing_extractor': {
            'model_id': 'openai/gpt-test-locked',
            'approved_fallbacks': [],
            'pricing': {'prompt': '0.000002', 'completion': '0.000006'},
        }},
    }), encoding='utf-8')
    env = {
        'AEGIS_ENABLE_PAID_LLM': 'true' if paid else 'false',
        'AEGIS_OPENROUTER_MODELS_LOCK': str(lock_file),
    }
    settings = AegisSettings.from_env(env)
    resolver = SecretResolver({'OPENROUTER_API_KEY': key} if key else {})
    budget = BudgetLedger(job_budget_usd=0.50, daily_budget_usd=1.00, project_budget_usd=2.00)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterGateway(settings, resolver, budget=budget, client=client)


def test_gateway_structured_success_and_privacy_flags(tmp_path: Path):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen['provider'] = body['provider']
        return httpx.Response(200, json={
            'model': 'openai/gpt-test-locked',
            'provider': 'OpenAI',
            'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'cost': 0.002},
            'choices': [{'message': {'content': json.dumps({
                'symbol': 'NVDA',
                'period': '2026Q3',
                'claims': ['Data center revenue increased'],
                'cited_evidence_ids': ['sec:1045810:1'],
                'abstain': False,
                'abstain_reason': None,
            })}}],
        })

    gw = _make_gateway(tmp_path, handler)
    res = gw.complete_structured(job_id='j1', role='filing_extractor', messages=[{'role': 'user', 'content': 'Extract'}])
    assert res['status'] == 'OK'
    assert res['cost_usd'] == 0.002
    assert seen['provider']['data_collection'] == 'deny'
    assert seen['provider']['zdr'] is True
    assert seen['provider']['allow_fallbacks'] is False


def test_gateway_error_classifications_and_no_blind_retry_on_timeout(tmp_path: Path):
    # Paid disabled -> BLOCKED_BUDGET
    gw_disabled = _make_gateway(tmp_path, lambda r: httpx.Response(200), paid=False)
    with pytest.raises(OpenRouterError) as e0:
        gw_disabled.complete_structured(job_id='j0', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
    assert e0.value.code == 'BLOCKED_BUDGET'

    # Missing key -> BLOCKED_CREDENTIAL
    gw_nokey = _make_gateway(tmp_path, lambda r: httpx.Response(200), key=None)
    with pytest.raises(OpenRouterError) as e1:
        gw_nokey.complete_structured(job_id='j1', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
    assert e1.value.code == 'BLOCKED_CREDENTIAL'

    # HTTP status codes
    for code, expected in [(401, 'INVALID_AUTH'), (402, 'BLOCKED_BUDGET'), (403, 'BLOCKED_PERMISSION'), (429, 'RATE_LIMITED')]:
        gw_err = _make_gateway(tmp_path, lambda r, c=code: httpx.Response(c, text='err'))
        with pytest.raises(OpenRouterError) as exc:
            gw_err.complete_structured(job_id=f'j_{code}', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
        assert exc.value.code == expected

    # Schema violation
    gw_bad = _make_gateway(tmp_path, lambda r: httpx.Response(200, json={'choices': [{'message': {'content': '{"bad": true}'}}]}))
    with pytest.raises(OpenRouterError) as e_sch:
        gw_bad.complete_structured(job_id='jsch', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
    assert e_sch.value.code == 'SCHEMA_VIOLATION'

    # Timeout after POST: must execute ONCE only and retain reservation
    calls = {'n': 0}

    def timeout_handler(req: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        raise httpx.ReadTimeout('timed out')

    gw_to = _make_gateway(tmp_path, timeout_handler)
    with pytest.raises(OpenRouterError) as e_to:
        gw_to.complete_structured(job_id='jto', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}], estimated_usd=0.05)
    assert e_to.value.code == 'UNKNOWN_OUTCOME'
    assert calls['n'] == 1
    assert gw_to.budget.snapshot()['unknown_outcomes'] == 1
    assert gw_to.budget.snapshot()['project_spent_usd'] == 0.05


def test_budget_persists_across_restart_and_resolves_unknown_outcome(tmp_path: Path):
    from aegis.store import Store

    db = tmp_path / 'budget.sqlite'
    b1 = BudgetLedger(job_budget_usd=0.20, daily_budget_usd=0.20, project_budget_usd=0.20, store=Store(db))
    b1._today = lambda: '2026-11-14'
    res = b1.reserve('job_a', 0.12)
    b1.reconcile(res.reservation_id, actual_usd=None, unknown_outcome=True)

    # Restart with fresh BudgetLedger instance against same SQLite file on next UTC day
    b2 = BudgetLedger(job_budget_usd=0.20, daily_budget_usd=0.20, project_budget_usd=0.20, store=Store(db))
    b2._today = lambda: '2026-11-15'
    assert b2.snapshot()['project_spent_usd'] == pytest.approx(0.12)
    assert b2.snapshot()['unknown_outcomes'] == 1

    # Exceeding project cap across restart fails
    with pytest.raises(BudgetExceeded):
        b2.reserve('job_b', 0.10)

    # Settle unknown outcome on 2026-11-15 -> adjusts originating day 2026-11-14
    settled = b2.reconcile(res.reservation_id, actual_usd=0.03)
    assert settled.status == 'SETTLED'
    assert b2.snapshot()['project_spent_usd'] == pytest.approx(0.03)
    assert b2.snapshot()['daily_spent_usd'] == pytest.approx(0.0)
    b2._today = lambda: '2026-11-14'
    assert b2.snapshot()['daily_spent_usd'] == pytest.approx(0.03)


def test_gateway_uses_pricing_from_lock_for_reservation(tmp_path: Path):
    lock_file = tmp_path / 'models.lock.json'
    lock_file.write_text(json.dumps({
        'schema_version': 1,
        'roles': {
            'filing_extractor': {
                'model_id': 'openai/gpt-test-locked',
                'approved_fallbacks': [],
                'pricing': {'prompt': '0.0001', 'completion': '0.0002'},
            },
        },
    }), encoding='utf-8')
    settings = AegisSettings.from_env({'AEGIS_ENABLE_PAID_LLM': 'true', 'AEGIS_OPENROUTER_MODELS_LOCK': str(lock_file)})
    resolver = SecretResolver({'OPENROUTER_API_KEY': 'sk-or-v1-test-key'})
    # High token-based estimate (1024 * 0.0002 = 0.2048) exceeds tiny 0.05 budget -> blocked before dispatch
    budget = BudgetLedger(job_budget_usd=0.05, daily_budget_usd=0.05, project_budget_usd=0.05)
    called = {'n': 0}

    def handler(req: httpx.Request) -> httpx.Response:
        called['n'] += 1
        return httpx.Response(200, json={})

    gw = OpenRouterGateway(settings, resolver, budget=budget, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OpenRouterError) as exc:
        gw.complete_structured(job_id='j_price', role='filing_extractor', messages=[{'role': 'user', 'content': 'Extract'}])
    assert exc.value.code == 'BLOCKED_BUDGET'
    assert called['n'] == 0


def test_f4_blocks_missing_or_stale_pricing_and_prevents_caller_undercut(tmp_path: Path):
    # 1. Missing pricing blocks dispatch with 0 HTTP calls
    lock_missing = tmp_path / 'lock_missing.json'
    lock_missing.write_text(json.dumps({
        'schema_version': 1,
        'roles': {'filing_extractor': {'model_id': 'openai/gpt-test', 'approved_fallbacks': []}},
    }), encoding='utf-8')
    called = {'n': 0}

    def handler(req: httpx.Request) -> httpx.Response:
        called['n'] += 1
        body = json.loads(req.content)
        called['provider'] = body['provider']
        return httpx.Response(200, json={
            'model': 'openai/gpt-test',
            'usage': {'cost': 0.002},
            'choices': [{'message': {'content': json.dumps({
                'symbol': 'NVDA', 'period': '2026Q3', 'claims': ['ok'],
                'cited_evidence_ids': ['sec:1'], 'abstain': False, 'abstain_reason': None,
            })}}],
        })

    gw_missing = OpenRouterGateway(
        AegisSettings.from_env({'AEGIS_ENABLE_PAID_LLM': 'true', 'AEGIS_OPENROUTER_MODELS_LOCK': str(lock_missing)}),
        SecretResolver({'OPENROUTER_API_KEY': 'sk-or-v1-test-key'}),
        budget=BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenRouterError) as exc:
        gw_missing.complete_structured(job_id='j_miss', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
    assert exc.value.code == 'BLOCKED_PRICING'
    assert called['n'] == 0

    # 2. Stale pricing blocks dispatch
    lock_stale = tmp_path / 'lock_stale.json'
    lock_stale.write_text(json.dumps({
        'schema_version': 1,
        'roles': {'filing_extractor': {
            'model_id': 'openai/gpt-test',
            'approved_fallbacks': [],
            'pricing': {'prompt': '0.000002', 'completion': '0.000006', 'updated_at': '2020-01-01T00:00:00Z'},
        }},
    }), encoding='utf-8')
    gw_stale = OpenRouterGateway(
        AegisSettings.from_env({'AEGIS_ENABLE_PAID_LLM': 'true', 'AEGIS_OPENROUTER_MODELS_LOCK': str(lock_stale)}),
        SecretResolver({'OPENROUTER_API_KEY': 'sk-or-v1-test-key'}),
        budget=BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenRouterError) as exc_stale:
        gw_stale.complete_structured(job_id='j_stale', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
    assert exc_stale.value.code == 'BLOCKED_PRICING'
    assert called['n'] == 0

    # 3. Caller cannot undercut computed bound, and max_price uses USD/1M tokens
    lock_ok = tmp_path / 'lock_ok.json'
    lock_ok.write_text(json.dumps({
        'schema_version': 1,
        'roles': {'filing_extractor': {
            'model_id': 'openai/gpt-test',
            'approved_fallbacks': [],
            'pricing': {'prompt': '0.000002', 'completion': '0.000006'},
        }},
    }), encoding='utf-8')
    gw_ok = OpenRouterGateway(
        AegisSettings.from_env({'AEGIS_ENABLE_PAID_LLM': 'true', 'AEGIS_OPENROUTER_MODELS_LOCK': str(lock_ok)}),
        SecretResolver({'OPENROUTER_API_KEY': 'sk-or-v1-test-key'}),
        budget=BudgetLedger(job_budget_usd=0.001, daily_budget_usd=1.0, project_budget_usd=1.0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    # Even if caller passes tiny estimated_usd=0.0001, computed bound (>0.006) exceeds job_budget_usd=0.001 -> blocked!
    with pytest.raises(OpenRouterError) as exc_under:
        gw_ok.complete_structured(job_id='j_under', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}], estimated_usd=0.0001)
    assert exc_under.value.code == 'BLOCKED_BUDGET'
    assert called['n'] == 0

    # With sufficient budget, max_price is converted to per-1M tokens (2.0 prompt, 6.0 completion)
    gw_pass = OpenRouterGateway(
        AegisSettings.from_env({'AEGIS_ENABLE_PAID_LLM': 'true', 'AEGIS_OPENROUTER_MODELS_LOCK': str(lock_ok)}),
        SecretResolver({'OPENROUTER_API_KEY': 'sk-or-v1-test-key'}),
        budget=BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    res = gw_pass.complete_structured(job_id='j_ok', role='filing_extractor', messages=[{'role': 'user', 'content': 'x'}])
    assert res['status'] == 'OK'
    assert called['provider']['max_price'] == {'prompt': 2.0, 'completion': 6.0}



