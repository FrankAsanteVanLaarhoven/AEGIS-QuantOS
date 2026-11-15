"""SEC EDGAR submissions and company-facts adapter with fair-access rate limiting."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from aegis.providers.evidence import SourceDocument
from aegis.settings import AegisSettings


class SecEdgarAdapter:
    BASE_URL = 'https://data.sec.gov'

    def __init__(self, settings: AegisSettings, min_interval_seconds: float = 0.12) -> None:
        self.settings = settings
        self.min_interval_seconds = min_interval_seconds
        self._last_request_ts: float = 0.0

    def _headers(self) -> dict[str, str]:
        ua = self.settings.sec_user_agent
        if not ua:
            raise ValueError('SEC_USER_AGENT must be set with operator name and real email before SEC requests')
        return {'User-Agent': ua, 'Accept': 'application/json'}

    def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_ts
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request_ts = time.monotonic()

    def fetch_submissions(self, cik: str, *, client: httpx.Client | None = None) -> tuple[dict[str, Any], list[SourceDocument]]:
        padded = ''.join(ch for ch in cik if ch.isdigit()).zfill(10)
        if not padded.strip('0'):
            raise ValueError('Valid numeric CIK required')
        headers = self._headers()
        self._throttle()
        url = f'{self.BASE_URL}/submissions/CIK{padded}.json'
        own = client is None
        http = client or httpx.Client(timeout=15.0)
        try:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if own:
                http.close()

        recent = (data.get('filings') or {}).get('recent') or {}
        forms = recent.get('form') or []
        dates = recent.get('filingDate') or []
        acc_dates = recent.get('acceptanceDateTime') or []
        accs = recent.get('accessionNumber') or []
        tickers = data.get('tickers') or []
        sym = tickers[0] if tickers else None
        docs: list[SourceDocument] = []
        for idx, (form, fdate, acc) in enumerate(zip(forms[:10], dates[:10], accs[:10])):
            pub_dt = datetime.fromisoformat(f'{fdate}T00:00:00+00:00')
            acc_raw = acc_dates[idx] if idx < len(acc_dates) else f'{fdate}T16:00:00Z'
            try:
                acc_dt = datetime.fromisoformat(str(acc_raw).replace('Z', '+00:00'))
                if acc_dt.tzinfo is None:
                    acc_dt = acc_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                acc_dt = pub_dt
            doc_uri = f'https://www.sec.gov/Archives/edgar/data/{int(padded)}/{acc.replace("-", "")}/'
            docs.append(
                SourceDocument.from_raw(
                    doc_id=f'sec:{padded}:{acc}',
                    uri=doc_uri,
                    provider='sec_edgar',
                    source_kind='sec_filing',
                    raw_text=f'SEC Form {form} filed {fdate} accession {acc} for {data.get("name", padded)}',
                    published_at=pub_dt,
                    accepted_at=acc_dt,
                    available_at=acc_dt,
                    licensing_tag='public_sec_edgar',
                    entity_symbol=sym,
                    entity_cik=padded,
                )
            )
        summary = {
            'cik': padded,
            'name': data.get('name'),
            'tickers': tickers,
            'response_sha256': hashlib.sha256(resp.content).hexdigest(),
            'filings_count': len(docs),
        }
        return summary, docs

    def fetch_company_facts(self, cik: str, *, client: httpx.Client | None = None) -> tuple[dict[str, Any], SourceDocument]:
        padded = ''.join(ch for ch in cik if ch.isdigit()).zfill(10)
        headers = self._headers()
        self._throttle()
        url = f'{self.BASE_URL}/api/xbrl/companyfacts/CIK{padded}.json'
        own = client is None
        http = client or httpx.Client(timeout=20.0)
        try:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if own:
                http.close()
        now = datetime.now(timezone.utc)
        facts = data.get('facts') or {}
        us_gaap = facts.get('us-gaap') or {}
        doc = SourceDocument.from_raw(
            doc_id=f'sec_facts:{padded}',
            uri=url,
            provider='sec_edgar',
            source_kind='sec_company_facts',
            raw_text=f'SEC XBRL companyfacts for {data.get("entityName", padded)} ({len(us_gaap)} us-gaap tags)',
            published_at=now,
            accepted_at=now,
            available_at=now,
            licensing_tag='public_sec_edgar',
            entity_cik=padded,
        )
        return {'cik': padded, 'entity_name': data.get('entityName'), 'us_gaap_tags': len(us_gaap)}, doc

