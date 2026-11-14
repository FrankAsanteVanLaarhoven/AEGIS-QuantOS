"""Licensed news adapter; fails closed without explicit provider entitlement."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from aegis.providers.evidence import SourceDocument
from aegis.secrets import SecretResolver


class LicensedNewsAdapter:
    DEFAULT_NEWS_URL = 'https://data.alpaca.markets/v1beta1/news'

    def __init__(self, resolver: SecretResolver, *, base_url: str | None = None) -> None:
        self.resolver = resolver
        self.base_url = (base_url or self.DEFAULT_NEWS_URL).rstrip('/')

    def status(self) -> dict[str, Any]:
        key = self.resolver.resolve('NEWS_API_KEY')
        if not key:
            return {
                'configured': False,
                'entitled': False,
                'state': 'BLOCKED_CREDENTIAL',
                'detail': 'No licensed news provider key configured; filings are not substituted for news',
            }
        return {
            'configured': True,
            'entitled': 'UNVERIFIED',
            'state': 'CONFIGURED_UNVERIFIED',
            'detail': 'Licensed news credential present; endpoint entitlement unverified',
        }

    def ingest_items(self, items: list[dict[str, Any]], *, provider: str = 'licensed_news', licensing_tag: str = 'commercial_licensed') -> list[SourceDocument]:
        if not self.resolver.resolve('NEWS_API_KEY'):
            raise ValueError('BLOCKED_CREDENTIAL: Licensed news credential required')
        docs: list[SourceDocument] = []
        for item in items:
            pub = datetime.fromisoformat(str(item['published_at']).replace('Z', '+00:00'))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            docs.append(
                SourceDocument.from_raw(
                    doc_id=str(item['id']),
                    uri=str(item['url']),
                    provider=provider,
                    source_kind='licensed_news',
                    raw_text=f"{item.get('headline', '')} — {item.get('summary', '')}",
                    published_at=pub,
                    accepted_at=pub,
                    available_at=pub,
                    licensing_tag=licensing_tag,
                    entity_symbol=item.get('symbol'),
                )
            )
        return docs

    def fetch_news(
        self,
        symbols: list[str],
        *,
        limit: int = 50,
        max_pages: int = 5,
        client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        key = self.resolver.resolve('NEWS_API_KEY')
        if not key:
            return {'status': 'BLOCKED_CREDENTIAL', 'documents': [], 'pages': 0}
        headers = {'Authorization': f'Bearer {key}'}
        own_client = client is None
        http = client or httpx.Client(timeout=15.0)
        all_items: list[dict[str, Any]] = []
        pages = 0
        page_token: str | None = None
        try:
            while pages < max_pages:
                params: dict[str, Any] = {'symbols': ','.join(s.upper() for s in symbols), 'limit': limit}
                if page_token:
                    params['page_token'] = page_token
                resp = http.get(self.base_url, headers=headers, params=params)
                if resp.status_code in (401, 403):
                    return {'status': 'BLOCKED_ENTITLEMENT', 'http_status': resp.status_code, 'documents': [], 'pages': pages}
                if resp.status_code == 429:
                    return {'status': 'RATE_LIMITED', 'http_status': 429, 'documents': [], 'pages': pages}
                resp.raise_for_status()
                body = resp.json() or {}
                pages += 1
                items = body.get('news') or body.get('items') or []
                for it in items:
                    syms = it.get('symbols') or []
                    sym = it.get('symbol') or (syms[0] if syms else (symbols[0].upper() if symbols else None))
                    all_items.append({
                        'id': it.get('id'),
                        'url': it.get('url') or it.get('uri'),
                        'headline': it.get('headline') or it.get('title') or '',
                        'summary': it.get('summary') or it.get('content') or '',
                        'published_at': it.get('created_at') or it.get('published_at'),
                        'symbol': sym,
                    })
                page_token = body.get('next_page_token')
                if not page_token:
                    break
        finally:
            if own_client:
                http.close()
        docs = self.ingest_items(all_items)
        return {'status': 'OK', 'documents': docs, 'pages': pages, 'count': len(docs)}


