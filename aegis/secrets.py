"""Isolated runtime secret resolution and redaction.

Reads only explicitly configured environment variables or *_FILE paths.
Never scans host directories, never returns plaintext secrets in APIs or logs.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PLACEHOLDER_PATTERNS = (
    'replace-with-',
    'paper-account-key',
    'paper-account-secret',
    'changeme',
    'your-key',
    'your-token',
    'your-secret',
    'example',
    'dummy',
)

MANAGED_SECRET_NAMES = (
    'AEGIS_TOKEN',
    'OPENROUTER_API_KEY',
    'ALPACA_API_KEY',
    'ALPACA_SECRET_KEY',
    'HF_TOKEN',
    'NEWS_API_KEY',
)


@dataclass(frozen=True)
class SecretStatus:
    name: str
    state: str  # CONFIGURED_ENV | CONFIGURED_FILE | MISSING | AMBIGUOUS | INVALID_FILE | INVALID_VALUE | REJECTED_MANAGEMENT_KEY
    source: str  # env | file | none
    detail: str


def _is_placeholder(value: str) -> bool:
    low = value.strip().lower()
    return any(p in low for p in PLACEHOLDER_PATTERNS)


def _validate_secret_value(name: str, value: str) -> tuple[bool, str, str]:
    stripped = value.strip()
    if not stripped:
        return False, 'INVALID_VALUE', 'Secret value is empty'
    if any(ch.isspace() for ch in stripped):
        return False, 'INVALID_VALUE', 'Secret value contains whitespace'
    if len(stripped) < 8:
        return False, 'INVALID_VALUE', 'Secret value is too short'
    if _is_placeholder(stripped):
        return False, 'INVALID_VALUE', 'Placeholder secret value rejected'
    if name == 'OPENROUTER_API_KEY' and stripped.startswith('sk-or-mgmt-'):
        return False, 'REJECTED_MANAGEMENT_KEY', 'OpenRouter management key cannot be used as runtime key'
    return True, 'OK', 'Valid'


def _read_private_file(path_str: str) -> tuple[str | None, str, str]:
    p = Path(path_str).expanduser()
    if p.is_symlink():
        return None, 'INVALID_FILE', 'Secret file must not be a symlink'
    if not p.exists():
        return None, 'INVALID_FILE', 'Secret file does not exist'
    if not p.is_file():
        return None, 'INVALID_FILE', 'Secret path is not a regular file'
    if os.name == 'posix':
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            return None, 'INVALID_FILE', f'Secret file permissions too broad ({oct(mode)}); require 0600 or 0400'
    raw = p.read_text(encoding='utf-8').strip()
    return raw, 'OK', 'Read private file'


class SecretResolver:
    """Resolves secrets from explicit env/file pairs without exposing values."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(env if env is not None else os.environ)
        self._cache: dict[str, str] = {}
        self._statuses: dict[str, SecretStatus] = {}

    def resolve(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        env_val = self._env.get(name, '').strip()
        file_val = self._env.get(f'{name}_FILE', '').strip()

        if env_val and file_val:
            self._statuses[name] = SecretStatus(
                name=name,
                state='AMBIGUOUS',
                source='both',
                detail=f'Both {name} and {name}_FILE are set; specify only one',
            )
            return None

        if file_val:
            raw, fstate, fdetail = _read_private_file(file_val)
            if raw is None:
                self._statuses[name] = SecretStatus(name=name, state=fstate, source='file', detail=fdetail)
                return None
            ok, vstate, vdetail = _validate_secret_value(name, raw)
            if not ok:
                self._statuses[name] = SecretStatus(name=name, state=vstate, source='file', detail=vdetail)
                return None
            self._cache[name] = raw
            self._statuses[name] = SecretStatus(name=name, state='CONFIGURED_FILE', source='file', detail='Configured via private file')
            return raw

        if env_val:
            ok, vstate, vdetail = _validate_secret_value(name, env_val)
            if not ok:
                self._statuses[name] = SecretStatus(name=name, state=vstate, source='env', detail=vdetail)
                return None
            self._cache[name] = env_val
            self._statuses[name] = SecretStatus(name=name, state='CONFIGURED_ENV', source='env', detail='Configured via environment')
            return env_val

        self._statuses[name] = SecretStatus(name=name, state='MISSING', source='none', detail='Not configured')
        return None

    def status(self, name: str) -> SecretStatus:
        if name not in self._statuses:
            self.resolve(name)
        return self._statuses[name]

    def summary(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for name in MANAGED_SECRET_NAMES:
            st = self.status(name)
            out[name] = {'state': st.state, 'source': st.source, 'detail': st.detail}
        return out

    def redact_text(self, text: str) -> str:
        redacted = text
        for name in MANAGED_SECRET_NAMES:
            val = self.resolve(name)
            if val:
                redacted = redacted.replace(val, '[REDACTED]')
        # Also redact common OpenRouter / Bearer token patterns
        redacted = re.sub(r'sk-or-(?:v1|mgmt)-[A-Za-z0-9_-]{8,}', '[REDACTED_OPENROUTER_KEY]', redacted)
        redacted = re.sub(r'Bearer\s+[A-Za-z0-9._-]{8,}', 'Bearer [REDACTED]', redacted)
        return redacted
