"""OpenRouter policy, model lock loader, and forbidden-tool enforcement."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_TOOLS = frozenset({'broker_submit', 'change_risk_policy', 'read_secret', 'arbitrary_shell'})


def load_openrouter_policy(policy_path: str | Path | None = None) -> dict[str, Any]:
    p = Path(policy_path) if policy_path else (ROOT / 'configs' / 'openrouter.policy.json')
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        p = ROOT / 'configs' / 'openrouter.policy.example.json'
    return json.loads(p.read_text(encoding='utf-8'))


def load_models_lock(lock_path: str | Path | None = None) -> dict[str, Any]:
    p = Path(lock_path) if lock_path else (ROOT / 'configs' / 'openrouter.models.lock.json')
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return {'schema_version': 1, 'status': 'UNQUALIFIED_NO_LOCK', 'roles': {}}
    return json.loads(p.read_text(encoding='utf-8'))


def validate_tool_allowlist(tools: list[str]) -> list[str]:
    bad = [t for t in tools if t in FORBIDDEN_TOOLS]
    if bad:
        raise ValueError(f'Forbidden LLM tools rejected: {bad}')
    return list(tools)


def resolve_role_model(role: str, policy: dict[str, Any], lock: dict[str, Any]) -> tuple[str | None, list[str]]:
    locked_role = (lock.get('roles') or {}).get(role) or {}
    model_id = locked_role.get('model_id') or ((policy.get('roles') or {}).get(role) or {}).get('model_id')
    if model_id in ('auto', 'openrouter/auto'):
        raise ValueError('BLOCKED_MODEL_CONFIGURATION: Unrestricted auto model routing is prohibited')
    fallbacks = locked_role.get('approved_fallbacks') or ((policy.get('roles') or {}).get(role) or {}).get('approved_fallbacks') or []
    return model_id, list(fallbacks)
