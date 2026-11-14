from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.jobs.queue import DurableJobManager
from aegis.providers.evidence import EvidenceStore, SourceDocument
from aegis.providers.news import LicensedNewsAdapter
from aegis.secrets import SecretResolver
from aegis.store import Store


def _authed_headers(client: TestClient, token: str) -> dict[str, str]:
    r = client.post('/api/login', json={'token': token})
    assert r.status_code == 200
    return {'X-Aegis-CSRF': r.json()['csrf']}


def test_agent_job_abstains_without_sourced_inputs_and_persists_across_restart(tmp_path):
    db_file = tmp_path / 'aegis.sqlite'
    app1 = create_app(str(db_file), token='tok-c5')
    with TestClient(app1) as c1:
        hdrs = _authed_headers(c1, 'tok-c5')
        # Without numerical_inputs or evidence, deterministic_baseline must abstain (no hardcoded fcf=120!)
        r_abs = c1.post('/api/agents/jobs', json={'symbol': 'NVDA', 'arm': 'deterministic_baseline'}, headers=hdrs)
        assert r_abs.status_code == 200
        j_abs = r_abs.json()
        assert j_abs['status'] == 'ABSTAINED'

        # With explicit numerical_inputs, job completes and persists
        r_ok = c1.post(
            '/api/agents/jobs',
            json={
                'symbol': 'NVDA',
                'arm': 'deterministic_baseline',
                'numerical_inputs': {'fcf': 120, 'growth': 0.08, 'discount': 0.11, 'terminal_growth': 0.03, 'years': 5, 'shares': 10},
            },
            headers=hdrs,
        )
        assert r_ok.status_code == 200
        j_ok = r_ok.json()
        assert j_ok['status'] == 'COMPLETED'
        assert j_ok['result']['numerical_outputs']['dcf']['per_share'] > 0

    # Restart app against same SQLite DB and verify jobs survived
    app2 = create_app(str(db_file), token='tok-c5')
    with TestClient(app2) as c2:
        _authed_headers(c2, 'tok-c5')
        jobs = c2.get('/api/agents/jobs').json()['jobs']
        ids = {j['job_id'] for j in jobs}
        assert j_abs['job_id'] in ids
        assert j_ok['job_id'] in ids


def test_job_manager_cancellation_and_evidence_store_persist(tmp_path):
    db = tmp_path / 'c5_store.sqlite'
    jm1 = DurableJobManager(store=Store(db))
    rec = jm1.create('research_arm', 'AAPL')
    jm1.cancel(rec.job_id)

    jm2 = DurableJobManager(store=Store(db))
    assert jm2.get(rec.job_id).status == 'CANCELLED'

    es1 = EvidenceStore(store=Store(db))
    doc = SourceDocument.from_raw(
        doc_id='sec:test:1',
        uri='https://www.sec.gov/example',
        provider='sec_edgar',
        source_kind='sec_filing',
        raw_text='Revenue increased 10% YoY.',
        published_at='2026-11-10T12:00:00Z',
        accepted_at='2026-11-10T12:01:00Z',
        available_at='2026-11-10T12:01:00Z',
        licensing_tag='public_sec',
        entity_symbol='AAPL',
    )
    es1.add(doc)

    es2 = EvidenceStore(store=Store(db))
    assert len(es2.all_documents()) == 1
    assert es2.all_documents()[0].entity_symbol == 'AAPL'


def test_news_adapter_http_fetch_pagination_and_error_classification():
    resolver = SecretResolver({'NEWS_API_KEY': 'news_key_test_1234'})
    adapter = LicensedNewsAdapter(resolver)

    def handler(req: httpx.Request) -> httpx.Response:
        tok = req.url.params.get('page_token')
        if tok is None:
            return httpx.Response(200, json={
                'news': [{'id': 'n1', 'url': 'https://news.example/1', 'headline': 'NVDA Q3', 'summary': 'Strong quarter', 'created_at': '2026-11-14T13:00:00Z', 'symbols': ['NVDA']}],
                'next_page_token': 'p2',
            })
        return httpx.Response(200, json={
            'news': [{'id': 'n2', 'url': 'https://news.example/2', 'headline': 'NVDA Guidance', 'summary': 'Raised outlook', 'created_at': '2026-11-14T14:00:00Z', 'symbols': ['NVDA']}],
            'next_page_token': None,
        })

    out = adapter.fetch_news(['NVDA'], client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out['status'] == 'OK'
    assert out['pages'] == 2
    assert out['count'] == 2
    assert out['documents'][0].entity_symbol == 'NVDA'

    err_403 = adapter.fetch_news(['NVDA'], client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))))
    assert err_403['status'] == 'BLOCKED_ENTITLEMENT'
