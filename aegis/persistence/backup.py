"""SQLite WAL-safe online backup, restore verification, and schema migration tracking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def ensure_migrations_table(db_path: Path) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations ('
            'version TEXT PRIMARY KEY, '
            'applied_at TEXT NOT NULL)'
        )
        existing = {row[0] for row in conn.execute('SELECT version FROM schema_migrations').fetchall()}
        if '0001_initial_ledger' not in existing:
            conn.execute(
                'INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)',
                ('0001_initial_ledger', datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
        return [row[0] for row in conn.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall()]


def backup_database(src_db: Path, backup_dir: Path) -> dict[str, Any]:
    if not src_db.exists():
        raise FileNotFoundError(f'Source database not found: {src_db}')
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest_db = backup_dir / f'aegis_backup_{ts}.sqlite3'
    with sqlite3.connect(str(src_db)) as src, sqlite3.connect(str(dest_db)) as dst:
        src.backup(dst)
    digest = hashlib.sha256(dest_db.read_bytes()).hexdigest()
    manifest = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_db': str(src_db),
        'backup_file': dest_db.name,
        'sha256': digest,
        'size_bytes': dest_db.stat().st_size,
    }
    (backup_dir / f'aegis_backup_{ts}.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return {'backup_path': str(dest_db), 'manifest': manifest}


def restore_and_verify(backup_db: Path, expected_sha256: str, target_db: Path) -> dict[str, Any]:
    actual = hashlib.sha256(backup_db.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f'Backup checksum mismatch: expected {expected_sha256}, got {actual}')
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(backup_db)) as src, sqlite3.connect(str(target_db)) as dst:
        src.backup(dst)
    with sqlite3.connect(str(target_db)) as check:
        integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise RuntimeError(f'Restored database integrity check failed: {integrity}')
    return {'restored_db': str(target_db), 'sha256_verified': True, 'integrity_check': integrity}
