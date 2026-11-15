"""Provider registry with explicit source kinds, licensing, and entitlement status."""
from __future__ import annotations

from typing import Any

from aegis.providers.news import LicensedNewsAdapter
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings


def provider_registry(settings: AegisSettings, resolver: SecretResolver) -> dict[str, dict[str, Any]]:
    news = LicensedNewsAdapter(resolver).status()
    return {
        'sec_edgar': {
            'source_kind': 'sec_filing',
            'configured': bool(settings.sec_user_agent),
            'requires_api_key': False,
            'licensing': 'public_sec_edgar',
            'state': 'CONFIGURED' if settings.sec_user_agent else 'BLOCKED_CONFIG',
        },
        'licensed_news': {
            'source_kind': 'licensed_news',
            **news,
        },
        'alpaca_data': {
            'source_kind': 'market_data',
            'feed': settings.alpaca_data_feed,
            'coverage': 'single_exchange' if settings.alpaca_data_feed == 'iex' else 'consolidated',
            'configured': resolver.status('ALPACA_API_KEY').state.startswith('CONFIGURED_') and resolver.status('ALPACA_SECRET_KEY').state.startswith('CONFIGURED_'),
        },
        'alpaca_paper': {
            'source_kind': 'paper_broker',
            'base_url': settings.alpaca_paper_base_url,
            'configured': resolver.status('ALPACA_API_KEY').state.startswith('CONFIGURED_') and resolver.status('ALPACA_SECRET_KEY').state.startswith('CONFIGURED_'),
        },
        'openrouter': {
            'source_kind': 'llm_research',
            'base_url': settings.openrouter_base_url,
            'configured': resolver.status('OPENROUTER_API_KEY').state.startswith('CONFIGURED_'),
            'paid_calls_authorized': settings.enable_paid_llm,
        },
    }

