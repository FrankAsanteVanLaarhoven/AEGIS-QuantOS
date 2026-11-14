from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.persistence import backup_database, ensure_migrations_table, restore_and_verify


def test_backup_restore_and_migrations(tmp_path: Path):
    db = tmp_path / 'aegis.sqlite3'
    app = create_app(db_path=str(db), token='backup-test-token-123456')
    client = TestClient(app)
    assert client.get('/api/health').status_code == 200
    assert client.get('/api/ready').status_code == 200

    migrations = ensure_migrations_table(db)
    assert '0001_initial_ledger' in migrations

    b = backup_database(db, tmp_path / 'backups')
    restored = restore_and_verify(Path(b['backup_path']), b['manifest']['sha256'], tmp_path / 'restored.sqlite3')
    assert restored['sha256_verified'] is True
    assert restored['integrity_check'] == 'ok'
