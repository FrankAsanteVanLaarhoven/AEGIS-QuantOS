"""Backend-only OpenRouter gateway with strict schemas, privacy routing, and budget enforcement."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from aegis.llm.budget import BudgetExceeded, BudgetLedger
from aegis.llm.registry import load_models_lock, load_openrouter_policy, resolve_role_model, validate_tool_allowlist
from aegis.llm.schemas import ROLE_SCHEMAS
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings


class OpenRouterError(RuntimeError):
    def __init__(self, code: str, detail: str, http_status: int | None = None) -> None:
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.detail = detail
        self.http_status = http_status


class OpenRouterGateway:
    def __init__(
        self,
        settings: AegisSettings,
        resolver: SecretResolver,
        *,
        budget: BudgetLedger | None = None,
        client: httpx.Client | None = None,
        store: Any = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.policy = load_openrouter_policy(settings.openrouter_policy_path)
        self.lock = load_models_lock(settings.openrouter_models_lock_path)
        self.budget = budget or BudgetLedger(
            job_budget_usd=self.policy.get('job_budget_usd'),
            daily_budget_usd=self.policy.get('daily_budget_usd'),
            project_budget_usd=self.policy.get('project_budget_usd'),
            store=store,
        )
        self._client = client

    def _require_runtime_key(self) -> str:
        key = self.resolver.resolve('OPENROUTER_API_KEY')
        if not key:
            st = self.resolver.status('OPENROUTER_API_KEY')
            raise OpenRouterError(
                'BLOCKED_CREDENTIAL' if st.state == 'MISSING' else st.state,
                st.detail,
            )
        return key

    def _headers(self, key: str) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://github.com/FrankAsanteVanLaarhoven/AEGIS-QuantOS',
            'X-Title': 'AEGIS-QuantOS',
        }

    def check_key(self) -> dict[str, Any]:
        key = self._require_runtime_key()
        url = f"{self.settings.openrouter_base_url.rstrip('/')}/key"
        own = self._client is None
        http = self._client or httpx.Client(timeout=15.0)
        try:
            resp = http.get(url, headers={'Authorization': f'Bearer {key}'})
            if resp.status_code == 401:
                raise OpenRouterError('INVALID_AUTH', 'OpenRouter key rejected (401)', 401)
            if resp.status_code == 403:
                raise OpenRouterError('BLOCKED_PERMISSION', 'OpenRouter key forbidden (403)', 403)
            resp.raise_for_status()
            data = (resp.json() or {}).get('data') or {}
            return {
                'authenticated': True,
                'limit': data.get('limit'),
                'limit_remaining': data.get('limit_remaining'),
                'usage': data.get('usage'),
                'is_free_tier': data.get('is_free_tier'),
            }
        finally:
            if own:
                http.close()

    def discover_models(self) -> list[dict[str, Any]]:
        url = f"{self.settings.openrouter_base_url.rstrip('/')}/models"
        own = self._client is None
        http = self._client or httpx.Client(timeout=20.0)
        try:
            resp = http.get(url)
            resp.raise_for_status()
            models = (resp.json() or {}).get('data') or []
        finally:
            if own:
                http.close()
        candidates = []
        for m in models:
            params = set(m.get('supported_parameters') or [])
            if {'tools', 'structured_outputs'}.issubset(params):
                candidates.append({
                    'id': m.get('id'),
                    'context_length': m.get('context_length'),
                    'pricing': m.get('pricing'),
                })
        return candidates

    def _estimate_reservation_usd(
        self,
        role: str,
        model_id: str,
        messages: list[dict[str, str]],
        explicit_usd: float | None,
    ) -> float:
        if explicit_usd is not None:
            est = float(explicit_usd)
            if not math.isfinite(est) or est <= 0.0:
                raise OpenRouterError('BLOCKED_BUDGET', 'estimated_usd must be finite and positive')
            return est
        role_cfg = ((self.policy.get('roles') or {}).get(role) or {})
        max_tokens = int(role_cfg.get('maximum_output_tokens', 1024))
        prompt_tokens = max(1, sum(len(str(m.get('content', ''))) for m in messages) // 3 + 64)
        role_lock = ((self.lock.get('roles') or {}).get(role) or {})
        pricing = role_lock.get('pricing') or ((self.lock.get('pricing') or {}).get(model_id))
        if isinstance(pricing, dict) and 'prompt' in pricing and 'completion' in pricing:
            p_rate = float(pricing['prompt'])
            c_rate = float(pricing['completion'])
            if math.isfinite(p_rate) and math.isfinite(c_rate) and p_rate >= 0.0 and c_rate >= 0.0:
                return max(1e-6, prompt_tokens * p_rate + max_tokens * c_rate)
        # Fallback conservative upper-bound scaled by configured max output tokens when lock lacks pricing
        return max(0.001, round((prompt_tokens + max_tokens) * 1e-5, 6))

    def complete_structured(
        self,
        *,
        job_id: str,
        role: str,
        messages: list[dict[str, str]],
        estimated_usd: float | None = None,
        allowed_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        if role not in ROLE_SCHEMAS:
            raise OpenRouterError('BLOCKED_MODEL_CONFIGURATION', f'Unknown role: {role}')
        if allowed_tools:
            validate_tool_allowlist(allowed_tools)
        if not self.settings.enable_paid_llm:
            raise OpenRouterError('BLOCKED_BUDGET', 'AEGIS_ENABLE_PAID_LLM is false')
        key = self._require_runtime_key()

        model_id, _fallbacks = resolve_role_model(role, self.policy, self.lock)
        if not model_id:
            raise OpenRouterError('BLOCKED_MODEL_CONFIGURATION', f'No locked model configured for role {role}')

        reserved_estimate = self._estimate_reservation_usd(role, model_id, messages, estimated_usd)
        try:
            res = self.budget.reserve(job_id, reserved_estimate)
        except (BudgetExceeded, ValueError) as exc:
            raise OpenRouterError('BLOCKED_BUDGET', str(exc)) from exc

        schema_cls: type[BaseModel] = ROLE_SCHEMAS[role]
        provider_prefs = self.policy.get('provider') or {}
        payload = {
            'model': model_id,
            'messages': messages,
            'max_tokens': ((self.policy.get('roles') or {}).get(role) or {}).get('maximum_output_tokens', 1024),
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': f'aegis_{role}',
                    'strict': True,
                    'schema': schema_cls.model_json_schema(),
                },
            },
            'provider': {
                'require_parameters': provider_prefs.get('require_parameters', True),
                'data_collection': provider_prefs.get('data_collection', 'deny'),
                'zdr': provider_prefs.get('zdr', True),
                'allow_fallbacks': provider_prefs.get('allow_fallbacks', False),
            },
        }
        url = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"
        own = self._client is None
        http = self._client or httpx.Client(timeout=float(self.policy.get('timeout_seconds', 30)))
        try:
            try:
                resp = http.post(url, headers=self._headers(key), json=payload)
            except httpx.TimeoutException as exc:
                # Ambiguous post-POST timeout: DO NOT blindly retry; mark unknown outcome
                self.budget.reconcile(res.reservation_id, actual_usd=None, unknown_outcome=True)
                raise OpenRouterError('UNKNOWN_OUTCOME', 'Request timed out after dispatch; not retried') from exc

            if resp.status_code == 401:
                self.budget.reconcile(res.reservation_id, actual_usd=0.0)
                raise OpenRouterError('INVALID_AUTH', 'OpenRouter rejected key (401)', 401)
            if resp.status_code == 402:
                self.budget.reconcile(res.reservation_id, actual_usd=0.0)
                raise OpenRouterError('BLOCKED_BUDGET', 'OpenRouter credit/key limit reached (402)', 402)
            if resp.status_code == 403:
                self.budget.reconcile(res.reservation_id, actual_usd=0.0)
                raise OpenRouterError('BLOCKED_PERMISSION', 'OpenRouter permission denied (403)', 403)
            if resp.status_code == 429:
                self.budget.reconcile(res.reservation_id, actual_usd=0.0)
                raise OpenRouterError('RATE_LIMITED', 'OpenRouter rate limit (429)', 429)
            if resp.status_code in (400, 404):
                self.budget.reconcile(res.reservation_id, actual_usd=0.0)
                raise OpenRouterError('BLOCKED_MODEL_CONFIGURATION', self.resolver.redact_text(resp.text[:300]), resp.status_code)
            resp.raise_for_status()

            body = resp.json()
            usage = body.get('usage') or {}
            actual_cost = usage.get('cost')
            if actual_cost is None:
                # Unknown cost is not zero: retain conservative estimate
                self.budget.reconcile(res.reservation_id, actual_usd=res.reserved_usd)
                recorded_cost = res.reserved_usd
            else:
                recorded_cost = float(actual_cost)
                self.budget.reconcile(res.reservation_id, actual_usd=recorded_cost)

            content = (((body.get('choices') or [{}])[0]).get('message') or {}).get('content', '')
            try:
                parsed_json = json.loads(content)
                validated = schema_cls.model_validate(parsed_json)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise OpenRouterError('SCHEMA_VIOLATION', f'Model output failed strict schema: {type(exc).__name__}') from exc

            return {
                'status': 'OK',
                'requested_model': model_id,
                'resolved_model': body.get('model'),
                'provider': body.get('provider'),
                'usage': usage,
                'cost_usd': recorded_cost,
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'output': validated.model_dump(mode='json'),
            }
        finally:
            if own:
                http.close()

