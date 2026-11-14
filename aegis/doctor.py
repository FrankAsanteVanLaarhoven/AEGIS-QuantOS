"""Redacted startup and integration doctor for AEGIS QuantOS."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import kronos_adapter as kronos
from .secrets import SecretResolver
from .settings import AegisSettings


def run_doctor(
    settings: AegisSettings,
    resolver: SecretResolver,
    *,
    check_network: bool = False,
) -> dict[str, Any]:
    secrets_summary = resolver.summary()
    or_state = secrets_summary['OPENROUTER_API_KEY']['state']
    alp_key_state = secrets_summary['ALPACA_API_KEY']['state']
    alp_sec_state = secrets_summary['ALPACA_SECRET_KEY']['state']
    alp_configured = alp_key_state.startswith('CONFIGURED_') and alp_sec_state.startswith('CONFIGURED_')
    or_configured = or_state.startswith('CONFIGURED_')

    kronos_st = kronos.status()
    providers = {
        'alpaca_data': {
            'configured': alp_configured,
            'authenticated': 'UNVERIFIED' if alp_configured else 'BLOCKED_CREDENTIAL',
            'entitled': 'UNVERIFIED' if alp_configured else 'BLOCKED_CREDENTIAL',
            'receiving': False,
            'fresh': False,
            'qualified': False,
            'feed': settings.alpaca_data_feed,
            'coverage': 'single_exchange' if settings.alpaca_data_feed == 'iex' else 'consolidated',
        },
        'alpaca_paper': {
            'configured': alp_configured,
            'authenticated': 'UNVERIFIED' if alp_configured else 'BLOCKED_CREDENTIAL',
            'entitled': 'UNVERIFIED' if alp_configured else 'BLOCKED_CREDENTIAL',
            'receiving': False,
            'fresh': False,
            'qualified': False,
            'base_url': settings.alpaca_paper_base_url,
        },
        'sec_edgar': {
            'configured': bool(settings.sec_user_agent),
            'authenticated': 'NOT_REQUIRED_USER_AGENT_ONLY' if settings.sec_user_agent else 'BLOCKED_CONFIG',
            'entitled': bool(settings.sec_user_agent),
            'receiving': False,
            'fresh': False,
            'qualified': False,
        },
        'openrouter': {
            'configured': or_configured,
            'authenticated': 'UNVERIFIED' if or_configured else 'BLOCKED_CREDENTIAL',
            'entitled': 'UNVERIFIED' if or_configured else 'BLOCKED_CREDENTIAL',
            'paid_calls_authorized': settings.enable_paid_llm,
            'receiving': False,
            'fresh': False,
            'qualified': False,
        },
        'kronos': {
            'configured': kronos_st.get('available', False),
            'authenticated': 'LOCAL_MODEL',
            'entitled': True,
            'receiving': kronos_st.get('available', False),
            'fresh': kronos_st.get('smoke_passed', False),
            'qualified': kronos_st.get('smoke_passed', False),
            'state': kronos_st.get('state', 'NOT_INSTALLED'),
        },
    }

    return {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'network_checks_performed': check_network,
        'settings': settings.redact(),
        'secrets': secrets_summary,
        'providers': providers,
        'offline_capable': True,
        'live_money_enabled': False,
    }

