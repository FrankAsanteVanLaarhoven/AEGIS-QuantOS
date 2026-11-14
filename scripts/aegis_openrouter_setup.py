#!/usr/bin/env python3
"""Bounded OpenRouter credential bootstrap; no secret values in stdout.

This utility does not install an AEGIS runtime LLM gateway. It prepares and checks
credentials for that implementation. Network actions run only on an explicit
subcommand. Management keys are never persisted or used for inference.
"""
from __future__ import annotations
import argparse
import getpass
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib import request, error

BASE = 'https://openrouter.ai/api/v1'
MAX_RESPONSE = 16 * 1024 * 1024
DEFAULT_KEY_FILE = Path.home() / '.config/aegis-quantos/secrets/openrouter.key'

class SetupError(RuntimeError):
    """Public error text must not include request headers or provider bodies."""

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError('REDIRECT_REJECTED')


def validate_secret(value: str) -> str:
    value = value.strip()
    if not 20 <= len(value) <= 2048 or any(c.isspace() for c in value):
        raise SetupError('INVALID_SECRET_FORMAT')
    if any(x in value.lower() for x in ('replace-with', 'your-api-key', '<token>')):
        raise SetupError('PLACEHOLDER_SECRET')
    return value


def read_secret_file(path: Path) -> str:
    path = path.expanduser().absolute()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        raise SetupError('SECRET_FILE_UNREADABLE') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise SetupError('INVALID_SECRET_FILE')
        if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise SetupError('SECRET_FILE_REQUIRES_OWNER_ONLY_PERMISSIONS')
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            fd = -1
            return validate_secret(stream.read(4097))
    finally:
        if fd >= 0:
            os.close(fd)


def get_secret(name: str, *, default_file: Path | None = None) -> str:
    direct, file = os.getenv(name), os.getenv(name + '_FILE')
    if direct and file:
        raise SetupError('AMBIGUOUS_SECRET_CONFIGURATION')
    if file:
        return read_secret_file(Path(file))
    if direct:
        return validate_secret(direct)
    if default_file and default_file.exists():
        return read_secret_file(default_file)
    raise SetupError('BLOCKED_CREDENTIAL')


def assert_private_destination(path: Path) -> Path:
    """Require a private parent outside any Git working tree. POSIX only."""
    if os.name != 'posix':
        raise SetupError('USE_OS_SECRET_MANAGER_ON_NON_POSIX_HOST')
    path = path.expanduser().absolute()
    if path.is_symlink() or path.exists():
        raise SetupError('SECRET_DESTINATION_ALREADY_EXISTS')
    parent = path.parent
    if any(p.is_symlink() for p in (parent, *parent.parents)):
        raise SetupError('SYMLINK_SECRET_PARENT_REJECTED')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SetupError('SECRET_DIRECTORY_REQUIRES_MODE_0700')
    try:
        result = subprocess.run(['git', '-C', str(parent), 'rev-parse', '--show-toplevel'],
                                capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError('CANNOT_VERIFY_SECRET_DESTINATION_OUTSIDE_GIT') from None
    if result.returncode == 0:
        raise SetupError('REFUSING_SECRET_STORAGE_INSIDE_GIT')
    return path


def write_secret(path: Path, value: str) -> None:
    value = validate_secret(value)
    path = assert_private_destination(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def request_json(method: str, endpoint: str, key: str | None, body=None, *, opener=None):
    allowed = {('GET', '/key'), ('GET', '/models'), ('POST', '/keys'),
               ('POST', '/chat/completions')}
    if (method, endpoint) not in allowed:
        if method != 'DELETE' or not re.fullmatch(r'/keys/[A-Za-z0-9_-]{8,160}', endpoint):
            raise SetupError('ENDPOINT_NOT_ALLOWLISTED')
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
               'X-OpenRouter-Title': 'AEGIS QuantOS credential preflight'}
    if key:
        headers['Authorization'] = 'Bearer ' + validate_secret(key)
    data = json.dumps(body, allow_nan=False).encode() if body is not None else None
    req = request.Request(BASE + endpoint, headers=headers, data=data, method=method)
    client = opener or request.build_opener(NoRedirect())
    try:
        with client.open(req, timeout=30) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise SetupError('PROVIDER_RESPONSE_TOO_LARGE')
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or parsed.get('error'):
                raise SetupError('PROVIDER_RESPONSE_INVALID')
            return parsed
    except error.HTTPError as exc:
        # Never print the provider body: it can echo sensitive input.
        raise SetupError(f'PROVIDER_HTTP_{exc.code}') from None
    except (error.URLError, TimeoutError, OSError):
        raise SetupError('PROVIDER_NETWORK_FAILURE_NO_AUTOMATIC_POST_RETRY') from None
    except (UnicodeError, json.JSONDecodeError):
        raise SetupError('PROVIDER_JSON_INVALID') from None


def check_runtime_key(key: str, *, transport=request_json) -> dict:
    info = transport('GET', '/key', key).get('data')
    if not isinstance(info, dict):
        raise SetupError('KEY_METADATA_INVALID')
    if info.get('is_management_key') or info.get('is_provisioning_key'):
        raise SetupError('MANAGEMENT_KEY_CANNOT_BE_RUNTIME_KEY')
    if info.get('disabled'):
        raise SetupError('KEY_DISABLED')
    expires = info.get('expires_at')
    if expires:
        try:
            expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise SetupError('KEY_EXPIRED')
        except (ValueError, TypeError):
            raise SetupError('KEY_EXPIRY_INVALID') from None
    fields = ('limit', 'limit_remaining', 'limit_reset', 'usage', 'usage_daily',
              'usage_monthly', 'expires_at', 'is_free_tier')
    return {'state': 'AUTHENTICATED', **{f: info.get(f) for f in fields}}


def create_runtime_key(management_key: str, destination: Path, limit_usd: float,
                       expires_days: int = 30, *, transport=request_json) -> dict:
    if not math.isfinite(limit_usd) or limit_usd <= 0:
        raise SetupError('POSITIVE_FINITE_USER_APPROVED_LIMIT_REQUIRED')
    if not 1 <= expires_days <= 90:
        raise SetupError('EXPIRY_MUST_BE_1_TO_90_DAYS')
    destination = assert_private_destination(destination)
    lock = destination.with_name(destination.name + '.provision.lock')
    try:
        lockfd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(lockfd)
    except FileExistsError:
        raise SetupError('PROVISIONING_ALREADY_IN_PROGRESS') from None
    issued = None
    try:
        # Lifetime cap, not a recurring allowance. No automatic credit purchase.
        body = {'name': 'AEGIS-QuantOS-research', 'limit': limit_usd,
                'limit_reset': None, 'include_byok_in_limit': True,
                'expires_at': (datetime.now(timezone.utc) + timedelta(days=expires_days)).strftime('%Y-%m-%dT%H:%M:%SZ')}
        result = transport('POST', '/keys', management_key, body)
        issued = result.get('data', {}).get('hash')
        value = validate_secret(result.get('key', ''))
        write_secret(destination, value)
        return {'state': 'KEY_CREATED_AND_SAVED', 'secret_file': str(destination),
                'limit_usd': limit_usd, 'limit_reset': None, 'expires_at': body['expires_at'],
                'next': 'Run verify, then explicitly authorised smoke. The application still needs the gateway implementation.'}
    except Exception as exc:
        if issued:
            try:
                transport('DELETE', '/keys/' + issued, management_key)
            except Exception:
                raise SetupError('PROVISIONING_PARTIAL_FAILURE_RECONCILE_KEYS_IN_PROVIDER_DASHBOARD') from None
        if isinstance(exc, SetupError):
            raise
        raise SetupError('KEY_STORAGE_FAILED') from None
    finally:
        lock.unlink(missing_ok=True)


def candidate_models(body: dict) -> list[dict]:
    rows = body.get('data')
    if not isinstance(rows, list):
        raise SetupError('MODEL_CATALOG_INVALID')
    selected = []
    for item in rows:
        params = item.get('supported_parameters') or []
        if 'structured_outputs' not in params or 'tools' not in params:
            continue
        selected.append({field: item.get(field) for field in
                         ('id', 'context_length', 'supported_parameters', 'pricing', 'expiration_date')})
    return selected


def smoke(key: str, model: str, *, allow_billable: bool = False, transport=request_json) -> dict:
    if not allow_billable:
        raise SetupError('EXPLICIT_BILLABLE_SMOKE_AUTHORIZATION_REQUIRED')
    if not re.fullmatch(r'[A-Za-z0-9._:-]+/[A-Za-z0-9._:/-]+', model):
        raise SetupError('INVALID_MODEL_ID')
    info = check_runtime_key(key, transport=transport)
    limit = info.get('limit')
    if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        raise SetupError('CAPPED_RUNTIME_KEY_REQUIRED_FOR_SMOKE')
    remaining = info.get('limit_remaining')
    if isinstance(remaining, (int, float)) and remaining <= 0:
        raise SetupError('KEY_BUDGET_EXHAUSTED')
    payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Return the JSON object {"ok": true}.'}],
               'max_tokens': 256, 'stream': False,
               'provider': {'require_parameters': True, 'data_collection': 'deny', 'zdr': True,
                            'allow_fallbacks': False},
               'response_format': {'type': 'json_schema', 'json_schema': {
                   'name': 'aegis_smoke', 'strict': True, 'schema': {'type': 'object',
                   'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}}}}
    result = transport('POST', '/chat/completions', key, payload)
    try:
        parsed = json.loads(result['choices'][0]['message']['content'])
    except (ValueError, TypeError, KeyError, IndexError):
        raise SetupError('SMOKE_SCHEMA_FAILED') from None
    if parsed != {'ok': True} or type(parsed['ok']) is not bool:
        raise SetupError('SMOKE_SCHEMA_FAILED')
    usage = result.get('usage') or {}
    return {'state': 'CREDENTIALED_INFERENCE_SMOKE_PASSED', 'requested_model': model,
            'resolved_model': result.get('model'),
            'usage': {k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cost')},
            'limitation': 'Credential smoke only; not financial-agent evaluation or application wiring.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status', help='Local presence check; no provider request')
    sub.add_parser('verify', help='Authenticate runtime key using GET /key; no inference')
    cat = sub.add_parser('catalog', help='Fetch model metadata; no inference')
    cat.add_argument('--output', type=Path)
    imp = sub.add_parser('import-key', help='Store an existing authorized runtime key outside Git')
    imp.add_argument('--destination', type=Path, default=DEFAULT_KEY_FILE)
    imp.add_argument('--prompt', action='store_true', help='Optional masked interactive entry on your own terminal')
    create = sub.add_parser('create-key', help='Create a capped runtime key using supplied management authorization')
    create.add_argument('--destination', type=Path, default=DEFAULT_KEY_FILE)
    create.add_argument('--limit-usd', required=True, type=float, help='User-approved lifetime cap; does not buy credits')
    create.add_argument('--expires-days', type=int, default=30)
    run = sub.add_parser('smoke', help='One small inference; may consume provider credits')
    run.add_argument('--model', required=True)
    run.add_argument('--allow-billable', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.cmd == 'status':
            get_secret('OPENROUTER_API_KEY', default_file=DEFAULT_KEY_FILE)
            result = {'state': 'SECRET_PRESENT_NOT_AUTHENTICATED'}
        elif args.cmd == 'create-key':
            result = create_runtime_key(get_secret('OPENROUTER_MANAGEMENT_KEY'), args.destination,
                                        args.limit_usd, args.expires_days)
        else:
            key = validate_secret(getpass.getpass('OpenRouter runtime key (hidden): ')) if args.cmd == 'import-key' and args.prompt else get_secret('OPENROUTER_API_KEY', default_file=DEFAULT_KEY_FILE)
            if args.cmd == 'verify':
                result = check_runtime_key(key)
            elif args.cmd == 'import-key':
                checked = check_runtime_key(key)
                write_secret(args.destination, key)
                result = {'state': 'KEY_IMPORTED', 'secret_file': str(args.destination), 'verification': checked}
            elif args.cmd == 'catalog':
                result = {'state': 'CATALOG_ONLY_NOT_BENCHMARKED', 'retrieved_at': datetime.now(timezone.utc).isoformat(),
                          'candidates': candidate_models(request_json('GET', '/models', key))}
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(result, indent=2) + '\n')
                    result = {'state': result['state'], 'candidate_count': len(result['candidates']), 'output': str(args.output)}
            else:
                result = smoke(key, args.model, allow_billable=args.allow_billable)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except SetupError as exc:
        print(json.dumps({'state': 'BLOCKED', 'reason': str(exc)}))
        return 2
    except Exception:
        print(json.dumps({'state': 'FAILED', 'reason': 'LOCAL_SETUP_ERROR_NO_SECRET_DETAILS_LOGGED'}))
        return 1

if __name__ == '__main__':
    sys.exit(main())
