from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.doctor import run_doctor
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings, load_dotenv_file


def test_secret_ambiguity_rejected(tmp_path: Path):
    kf = tmp_path / 'or.key'
    kf.write_text('sk-or-v1-validtestsecret123456', encoding='utf-8')
    os.chmod(kf, 0o600)
    resolver = SecretResolver({
        'OPENROUTER_API_KEY': 'sk-or-v1-envsecret123456',
        'OPENROUTER_API_KEY_FILE': str(kf),
    })
    assert resolver.resolve('OPENROUTER_API_KEY') is None
    assert resolver.status('OPENROUTER_API_KEY').state == 'AMBIGUOUS'


def test_secret_file_rejects_symlink_and_broad_permissions(tmp_path: Path):
    real = tmp_path / 'real.key'
    real.write_text('sk-or-v1-validtestsecret123456', encoding='utf-8')
    os.chmod(real, 0o644)
    r_broad = SecretResolver({'OPENROUTER_API_KEY_FILE': str(real)})
    assert r_broad.resolve('OPENROUTER_API_KEY') is None
    assert r_broad.status('OPENROUTER_API_KEY').state == 'INVALID_FILE'

    os.chmod(real, 0o600)
    link = tmp_path / 'link.key'
    link.symlink_to(real)
    r_link = SecretResolver({'OPENROUTER_API_KEY_FILE': str(link)})
    assert r_link.resolve('OPENROUTER_API_KEY') is None
    assert r_link.status('OPENROUTER_API_KEY').state == 'INVALID_FILE'

    r_ok = SecretResolver({'OPENROUTER_API_KEY_FILE': str(real)})
    assert r_ok.resolve('OPENROUTER_API_KEY') == 'sk-or-v1-validtestsecret123456'
    assert r_ok.status('OPENROUTER_API_KEY').state == 'CONFIGURED_FILE'


def test_rejects_management_key_and_placeholder():
    r1 = SecretResolver({'OPENROUTER_API_KEY': 'sk-or-mgmt-1234567890abcdef'})
    assert r1.resolve('OPENROUTER_API_KEY') is None
    assert r1.status('OPENROUTER_API_KEY').state == 'REJECTED_MANAGEMENT_KEY'

    r2 = SecretResolver({'ALPACA_API_KEY': 'paper-account-key'})
    assert r2.resolve('ALPACA_API_KEY') is None
    assert r2.status('ALPACA_API_KEY').state == 'INVALID_VALUE'


def test_redaction_strips_secrets():
    secret = 'sk-or-v1-supersecret987654321'
    r = SecretResolver({'OPENROUTER_API_KEY': secret})
    msg = f'Failed request with Authorization: Bearer {secret} and raw {secret}'
    red = r.redact_text(msg)
    assert secret not in red
    assert '[REDACTED' in red


def test_settings_enforce_paper_only_and_official_hosts():
    with pytest.raises(ValueError):
        AegisSettings.from_env({'AEGIS_ALLOW_LIVE_MONEY': 'true'})
    with pytest.raises(ValueError):
        AegisSettings.from_env({'ALPACA_PAPER_BASE_URL': 'https://api.alpaca.markets'})
    with pytest.raises(ValueError):
        AegisSettings.from_env({'OPENROUTER_BASE_URL': 'https://evil.example.com/v1'})


def test_dotenv_ignores_management_keys(tmp_path: Path, monkeypatch):
    env_file = tmp_path / '.env'
    env_file.write_text('AEGIS_PORT=8777\nOPENROUTER_MANAGEMENT_KEY=sk-or-mgmt-abc12345\n', encoding='utf-8')
    monkeypatch.delenv('OPENROUTER_MANAGEMENT_KEY', raising=False)
    loaded = load_dotenv_file(env_file)
    assert 'OPENROUTER_MANAGEMENT_KEY' not in loaded
    assert os.environ.get('OPENROUTER_MANAGEMENT_KEY') is None


def test_doctor_and_api_never_leak_secrets(tmp_path: Path, monkeypatch):
    secret = 'sk-or-v1-mysecretkey1234567890'
    monkeypatch.setenv('OPENROUTER_API_KEY', secret)
    monkeypatch.delenv('OPENROUTER_API_KEY_FILE', raising=False)

    settings = AegisSettings.from_env({'AEGIS_DATA_MODE': 'live', 'AEGIS_EXECUTION_MODE': 'paper_local'})
    resolver = SecretResolver()
    doc = run_doctor(settings, resolver, check_network=False)
    assert doc['providers']['openrouter']['configured'] is True
    assert doc['providers']['openrouter']['authenticated'] == 'UNVERIFIED'
    assert secret not in json.dumps(doc)

    app = create_app(db_path=str(tmp_path / 'aegis.sqlite3'), token='test-token-12345678')
    client = TestClient(app)
    res = client.post('/api/login', json={'token': 'test-token-12345678'})
    assert res.status_code == 200
    d_res = client.get('/api/doctor')
    assert d_res.status_code == 200
    assert secret not in d_res.text
    i_res = client.get('/api/integrations')
    assert i_res.status_code == 200
    assert secret not in i_res.text
    r_res = client.get('/api/ready')
    assert r_res.status_code == 200
    assert r_res.json()['live_execution'] is False

