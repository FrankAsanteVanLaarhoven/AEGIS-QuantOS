"""Typed public configuration for AEGIS QuantOS.

Separates data_mode from execution_mode, enforces paper-only execution,
and binds SecretResolver without ever exposing secret values.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Mapping
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .secrets import SecretResolver

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ALPACA_PAPER_HOSTS = ('https://paper-api.alpaca.markets',)
ALLOWED_OPENROUTER_URLS = ('https://openrouter.ai/api/v1',)


def load_dotenv_file(env_path: Path | None = None) -> dict[str, str]:
    """Load key=value pairs from .env without overwriting existing env vars."""
    target = env_path or (ROOT / '.env')
    loaded: dict[str, str] = {}
    if not target.exists() or not target.is_file():
        return loaded
    for raw in target.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key == 'OPENROUTER_MANAGEMENT_KEY' or key == 'OPENROUTER_MANAGEMENT_KEY_FILE':
            continue  # Management keys must never be loaded into application runtime
        loaded[key] = val
        os.environ.setdefault(key, val)
    return loaded


def _parse_bool(val: str | bool | None, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return val.strip().lower() in {'1', 'true', 'yes', 'on'}


class AegisSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')

    port: int = Field(default=8765, ge=1024, le=65535)
    host: str = Field(default='127.0.0.1')
    data_mode: Literal['synthetic', 'historical', 'delayed', 'live'] = 'synthetic'
    execution_mode: Literal['paper_local', 'paper_external', 'live_disabled'] = 'paper_local'
    allow_live_money: Literal[False] = False
    allow_synthetic_fallback: bool = False
    alpaca_data_feed: Literal['iex', 'sip'] = 'iex'
    alpaca_paper_base_url: str = 'https://paper-api.alpaca.markets'
    alpaca_data_base_url: str = 'https://data.alpaca.markets'
    alpaca_stream_url: str = 'wss://stream.data.alpaca.markets/v2'
    llm_provider: Literal['openrouter', 'disabled'] = 'openrouter'
    enable_paid_llm: bool = False
    openrouter_base_url: str = 'https://openrouter.ai/api/v1'
    openrouter_policy_path: str = 'configs/openrouter.policy.json'
    openrouter_models_lock_path: str = 'configs/openrouter.models.lock.json'
    sec_user_agent: str | None = None
    model_device: Literal['cpu', 'cuda', 'auto'] = 'cpu'
    models_dir: Path = Field(default_factory=lambda: ROOT / 'models')
    db_path: Path = Field(default_factory=lambda: ROOT / 'runtime' / 'aegis.sqlite3')
    token_ttl_seconds: int = Field(default=28800, ge=300, le=86400)

    @field_validator('alpaca_paper_base_url')
    @classmethod
    def _validate_paper_url(cls, v: str) -> str:
        clean = v.rstrip('/')
        if clean not in ALLOWED_ALPACA_PAPER_HOSTS:
            raise ValueError(f'Only paper broker endpoint is permitted: {ALLOWED_ALPACA_PAPER_HOSTS}')
        return clean

    @field_validator('openrouter_base_url')
    @classmethod
    def _validate_openrouter_url(cls, v: str) -> str:
        clean = v.rstrip('/')
        if clean not in ALLOWED_OPENROUTER_URLS:
            raise ValueError(f'Only official OpenRouter HTTPS endpoint is permitted: {ALLOWED_OPENROUTER_URLS}')
        return clean

    @field_validator('sec_user_agent')
    @classmethod
    def _validate_sec_ua(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        low = s.lower()
        if 'example.com' in low or 'your-real-email' in low or '@' not in s:
            raise ValueError('SEC_USER_AGENT must contain an actual operator name and contact email')
        return s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> 'AegisSettings':
        e = env if env is not None else os.environ
        allow_live = _parse_bool(e.get('AEGIS_ALLOW_LIVE_MONEY'), False)
        if allow_live:
            raise ValueError('AEGIS_ALLOW_LIVE_MONEY=true is prohibited in this release')
        sec_ua = e.get('SEC_USER_AGENT', '').strip() or None
        # Allow deferred validation error handling via explicit None if placeholder in .env.example
        if sec_ua and ('example.com' in sec_ua.lower() or 'your-real-email' in sec_ua.lower()):
            sec_ua = None
        return cls(
            port=int(e.get('AEGIS_PORT', '8765')),
            host=e.get('AEGIS_HOST', '127.0.0.1'),
            data_mode=e.get('AEGIS_DATA_MODE', 'synthetic'),
            execution_mode=e.get('AEGIS_EXECUTION_MODE', 'paper_local'),
            allow_live_money=False,
            allow_synthetic_fallback=_parse_bool(e.get('AEGIS_SYNTHETIC_FALLBACK'), False),
            alpaca_data_feed=e.get('ALPACA_DATA_FEED', 'iex'),
            alpaca_paper_base_url=e.get('ALPACA_PAPER_BASE_URL', 'https://paper-api.alpaca.markets'),
            llm_provider=e.get('AEGIS_LLM_PROVIDER', 'openrouter'),
            enable_paid_llm=_parse_bool(e.get('AEGIS_ENABLE_PAID_LLM'), False),
            openrouter_base_url=e.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
            openrouter_policy_path=e.get('AEGIS_OPENROUTER_POLICY', 'configs/openrouter.policy.json'),
            openrouter_models_lock_path=e.get('AEGIS_OPENROUTER_MODELS_LOCK', 'configs/openrouter.models.lock.json'),
            sec_user_agent=sec_ua,
            model_device=e.get('AEGIS_MODEL_DEVICE', 'cpu'),
            models_dir=Path(e.get('AEGIS_MODELS_DIR', str(ROOT / 'models'))),
            db_path=Path(e.get('AEGIS_DB', str(ROOT / 'runtime' / 'aegis.sqlite3'))),
            token_ttl_seconds=int(e.get('AEGIS_TOKEN_TTL_SECONDS', '28800')),
        )

    def redact(self) -> dict[str, object]:
        return {
            'port': self.port,
            'host': self.host,
            'data_mode': self.data_mode,
            'execution_mode': self.execution_mode,
            'allow_live_money': self.allow_live_money,
            'allow_synthetic_fallback': self.allow_synthetic_fallback,
            'alpaca_data_feed': self.alpaca_data_feed,
            'alpaca_data_coverage': 'single_exchange' if self.alpaca_data_feed == 'iex' else 'consolidated',
            'alpaca_paper_base_url': self.alpaca_paper_base_url,
            'llm_provider': self.llm_provider,
            'enable_paid_llm': self.enable_paid_llm,
            'openrouter_base_url': self.openrouter_base_url,
            'sec_user_agent_configured': bool(self.sec_user_agent),
            'model_device': self.model_device,
            'models_dir': str(self.models_dir),
            'token_ttl_seconds': self.token_ttl_seconds,
        }


def build_runtime_context(env: Mapping[str, str] | None = None) -> tuple[AegisSettings, SecretResolver]:
    if env is None:
        load_dotenv_file()
    settings = AegisSettings.from_env(env)
    resolver = SecretResolver(env)
    return settings, resolver

