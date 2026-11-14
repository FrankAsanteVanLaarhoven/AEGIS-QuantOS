"""Licensed news adapter; fails closed without explicit provider entitlement."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aegis.providers.evidence import SourceDocument
from aegis.secrets import SecretResolver


class LicensedNewsAdapter:
    def __init__(self, resolver: SecretResolver) -> None:
        self.resolver = resolver

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
