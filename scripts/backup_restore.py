#!/usr/bin/env python3
"""Create a verified SQLite backup and test-restore it."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aegis.persistence import backup_database, ensure_migrations_table, restore_and_verify


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=Path, default=ROOT / 'runtime' / 'aegis.sqlite3')
    p.add_argument('--backup-dir', type=Path, default=ROOT / 'runtime' / 'backups')
    p.add_argument('--verify-restore', action='store_true')
    args = p.parse_args(argv)

    migrations = ensure_migrations_table(args.db)
    b = backup_database(args.db, args.backup_dir)
    out = {'migrations': migrations, **b}
    if args.verify_restore:
        scratch = args.backup_dir / 'restore_verify.sqlite3'
        r = restore_and_verify(Path(b['backup_path']), b['manifest']['sha256'], scratch)
        out['restore_verification'] = r
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

