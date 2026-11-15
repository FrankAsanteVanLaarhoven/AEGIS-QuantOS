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

    @staticmethod
    def per_token_to_per_million(rate_per_token: float) -> float:
        """Convert OpenRouter catalog USD/token rate to routing max_price USD/1M tokens."""
        return round(float(rate_per_token) * 1_000_000.0, 6)

    def _resolve_pricing(self, role: str, model_id: str) -> tuple[float, float, float]:
        role_lock = ((self.lock.get('roles') or {}).get(role) or {})
        pricing = role_lock.get('pricing') or ((self.lock.get('pricing') or {}).get(model_id))
        if not isinstance(pricing, dict) or 'prompt' not in pricing or 'completion' not in pricing:
            raise OpenRouterError('BLOCKED_PRICING', f'Missing qualified pricing for {role} ({model_id}); billable dispatch blocked')
        try:
            p_rate = float(pricing['prompt'])
            c_rate = float(pricing['completion'])
            req_charge = float(pricing.get('request') or 0.0)
        except (TypeError, ValueError) as exc:
            raise OpenRouterError('BLOCKED_PRICING', f'Invalid pricing format for {model_id}') from exc
        if not (math.isfinite(p_rate) and math.isfinite(c_rate) and math.isfinite(req_charge)) or p_rate < 0.0 or c_rate < 0.0 or req_charge < 0.0:
            raise OpenRouterError('BLOCKED_PRICING', f'Non-finite or negative pricing for {model_id}')
        if p_rate == 0.0 and c_rate == 0.0 and req_charge == 0.0 and not pricing.get('free'):
            raise OpenRouterError('BLOCKED_PRICING', f'Zero pricing without explicit free-tier qualification for {model_id}')
        updated_raw = pricing.get('updated_at')
        if not updated_raw:
            raise OpenRouterError('BLOCKED_PRICING', f'Missing pricing updated_at provenance for {model_id}')
        try:
            updated_dt = datetime.fromisoformat(str(updated_raw).replace('Z', '+00:00'))
            max_age_h = float(self.policy.get('max_pricing_age_hours', 168.0))
            age_h = (datetime.now(timezone.utc) - updated_dt.astimezone(timezone.utc)).total_seconds() / 3600.0
            if age_h > max_age_h:
                raise OpenRouterError('BLOCKED_PRICING', f'Stale pricing ({age_h:.1f}h > {max_age_h:.1f}h) for {model_id}')
        except ValueError as exc:
            raise OpenRouterError('BLOCKED_PRICING', f'Invalid pricing updated_at timestamp for {model_id}') from exc
        max_p_pm = self.policy.get('max_prompt_price_per_million')
        if max_p_pm is not None and self.per_token_to_per_million(p_rate) > float(max_p_pm):
            raise OpenRouterError('BLOCKED_PRICING', f'Prompt price exceeds configured unit cap for {model_id}')
        max_c_pm = self.policy.get('max_completion_price_per_million')
        if max_c_pm is not None and self.per_token_to_per_million(c_rate) > float(max_c_pm):
            raise OpenRouterError('BLOCKED_PRICING', f'Completion price exceeds configured unit cap for {model_id}')
        return p_rate, c_rate, req_charge

    def _estimate_reservation_usd(
        self,
        role: str,
        model_id: str,
        messages: list[dict[str, Any]],
        explicit_usd: float | None,
        *,
        response_format: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> tuple[float, dict[str, float]]:
        p_rate, c_rate, req_charge = self._resolve_pricing(role, model_id)
        for msg in messages:
            if not isinstance(msg, dict) or not isinstance(msg.get('content'), str):
                raise OpenRouterError('BLOCKED_INPUT_UNBOUNDED', 'Only text-string message contents have a proven token bound')
        role_cfg = ((self.policy.get('roles') or {}).get(role) or {})
        max_tokens = int(role_cfg.get('maximum_output_tokens', 1024))
        if max_tokens <= 0 or max_tokens > 32768:
            raise OpenRouterError('BLOCKED_MODEL_CONFIGURATION', f'Invalid maximum_output_tokens={max_tokens}')
        serialized = json.dumps(
            {'messages': messages, 'response_format': response_format or {}, 'tools': allowed_tools or []},
            sort_keys=True,
            separators=(',', ':'),
        )
        # Provable UTF-8 byte upper-bound: every BPE token encodes >= 1 UTF-8 byte (+ 128 framing tokens).
        utf8_bytes = len(serialized.encode('utf-8'))
        input_tokens_bound = utf8_bytes + 128
        role_lock = ((self.lock.get('roles') or {}).get(role) or {})
        ctx_limit = role_lock.get('context_length') or self.lock.get('context_length')
        if ctx_limit is not None and (input_tokens_bound + max_tokens) > int(ctx_limit):
            raise OpenRouterError('BLOCKED_CONTEXT_LIMIT', f'Bound {input_tokens_bound + max_tokens} exceeds context_length {ctx_limit}')
        computed_bound = max(1e-6, round(input_tokens_bound * p_rate + max_tokens * c_rate + req_charge, 9))
        if explicit_usd is not None:
            est = float(explicit_usd)
            if not math.isfinite(est) or est <= 0.0:
                raise OpenRouterError('BLOCKED_BUDGET', 'estimated_usd must be finite and positive')
            # Caller may increase the reservation, never undercut the computed conservative bound.
            final_est = max(computed_bound, est)
        else:
            final_est = computed_bound
        max_req_usd = self.policy.get('max_request_usd')
        if max_req_usd is not None and final_est > float(max_req_usd):
            raise OpenRouterError('BLOCKED_BUDGET', f'Request bound {final_est} exceeds max_request_usd {max_req_usd}')
        max_key_spend = self.policy.get('max_key_spend_usd')
        if max_key_spend is not None:
            spent_now = float(self.budget.snapshot().get('project_spent_usd') or 0.0)
            if spent_now + final_est > float(max_key_spend):
                raise OpenRouterError('BLOCKED_BUDGET', 'Operator-approved key spend limit exceeded')
        max_price = {
            'prompt': self.per_token_to_per_million(p_rate),
            'completion': self.per_token_to_per_million(c_rate),
        }
        return final_est, max_price

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

        schema_cls: type[BaseModel] = ROLE_SCHEMAS[role]
        response_format = {
            'type': 'json_schema',
            'json_schema': {
                'name': f'aegis_{role}',
                'strict': True,
                'schema': schema_cls.model_json_schema(),
            },
        }
        reserved_estimate, max_price = self._estimate_reservation_usd(
            role,
            model_id,
            messages,
            estimated_usd,
            response_format=response_format,
            allowed_tools=allowed_tools,
        )
        try:
            res = self.budget.reserve(job_id, reserved_estimate)
        except (BudgetExceeded, ValueError) as exc:
            raise OpenRouterError('BLOCKED_BUDGET', str(exc)) from exc

        provider_prefs = self.policy.get('provider') or {}
        provider_routing: dict[str, Any] = {
            'require_parameters': provider_prefs.get('require_parameters', True),
            'data_collection': provider_prefs.get('data_collection', 'deny'),
            'zdr': provider_prefs.get('zdr', True),
            'allow_fallbacks': provider_prefs.get('allow_fallbacks', False),
        }
        if max_price['prompt'] > 0 or max_price['completion'] > 0:
            provider_routing['max_price'] = max_price
        payload = {
            'model': model_id,
            'messages': messages,
            'max_tokens': ((self.policy.get('roles') or {}).get(role) or {}).get('maximum_output_tokens', 1024),
            'response_format': response_format,
            'provider': provider_routing,
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
            gen_id = body.get('id') or resp.headers.get('x-request-id')
            actual_cost = usage.get('cost')
            if actual_cost is None:
                # Missing provider cost MUST remain explicitly unresolved until reconciled; never settle as observed.
                self.budget.reconcile(res.reservation_id, actual_usd=None, unknown_outcome=True, provider_request_id=gen_id)
                recorded_cost: float | None = None
                cost_status = 'UNRESOLVED'
            else:
                recorded_cost = float(actual_cost)
                self.budget.reconcile(res.reservation_id, actual_usd=recorded_cost, provider_request_id=gen_id)
                cost_status = 'OBSERVED'

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
                'generation_id': gen_id,
                'reservation_id': res.reservation_id,
                'usage': usage,
                'cost_usd': recorded_cost,
                'cost_status': cost_status,
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'output': validated.model_dump(mode='json'),
            }
        finally:
            if own:
                http.close()

