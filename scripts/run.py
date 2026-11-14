#!/usr/bin/env python3
"""Start a single-operator, loopback-only paper workspace."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aegis.settings import build_runtime_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--show-token', action='store_true', help='Print local workspace token for interactive enrolment')
    args = parser.parse_args(argv)

    settings, resolver = build_runtime_context()
    runtime_dir = ROOT / 'runtime'
    runtime_dir.mkdir(exist_ok=True)
    token_path = runtime_dir / 'access.token'

    resolved_token = resolver.resolve('AEGIS_TOKEN')
    if not resolved_token:
        if not token_path.exists():
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(secrets.token_urlsafe(32))
        os.environ['AEGIS_TOKEN_FILE'] = str(token_path)
        resolver = build_runtime_context()[1]
        resolved_token = resolver.resolve('AEGIS_TOKEN')

    print('\nAEGIS QuantOS — local paper-trading workspace', flush=True)
    print(f'Open: http://{settings.host}:{settings.port}', flush=True)
    print(f'Data mode: {settings.data_mode} | Execution mode: {settings.execution_mode} | Live money: {settings.allow_live_money}', flush=True)
    if args.show_token and resolved_token:
        print(f'Workspace token: {resolved_token}', flush=True)
    else:
        print(f'Workspace token stored in private file: {token_path} (pass --show-token to display)', flush=True)
    print('No live execution. Use Ctrl+C to stop.\n', flush=True)

    import uvicorn
    from aegis.api import create_app

    uvicorn.run(
        create_app(db_path=str(settings.db_path), token=resolved_token),
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level='warning',
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
