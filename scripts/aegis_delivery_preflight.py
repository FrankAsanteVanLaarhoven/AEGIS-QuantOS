#!/usr/bin/env python3
"""Read-only, redacted local AEGIS handoff preflight. No external API calls."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone

BASELINE = 'b0ec80cbea1e2a78155525fed2b13542d523e9e7'
REPO = 'FrankAsanteVanLaarhoven/AEGIS-QuantOS'
REQUIRED = ['README.md', 'aegis/api.py', 'aegis/engine.py', 'aegis/connectors.py',
            'aegis/kronos_adapter.py', 'scripts/run.py', 'web/index.html', 'tests']


def git(root: Path, *args):
    try:
        p = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                           text=True, timeout=10, check=False)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def secret_presence(name):
    # Report presence only, not values or discovered paths. No host credential search.
    direct, file = os.getenv(name), os.getenv(name + '_FILE')
    if direct and file:
        return 'AMBIGUOUS_CONFIGURATION'
    if direct:
        return 'PRESENT_UNVERIFIED'
    if file:
        try:
            return 'FILE_PRESENT_UNVERIFIED' if Path(file).expanduser().is_file() else 'CONFIGURED_FILE_MISSING'
        except OSError:
            return 'CONFIGURED_FILE_UNREADABLE'
    return 'NOT_CONFIGURED'


def inspect(root: Path):
    root = root.resolve()
    head = git(root, 'rev-parse', 'HEAD')
    origin = git(root, 'remote', 'get-url', 'origin')
    # Do not print remote strings: they can contain embedded credentials.
    origin_matches = bool(origin and (origin.rstrip('/').removesuffix('.git').endswith('/' + REPO)
                                      or origin.rstrip('/').removesuffix('.git').endswith(':' + REPO)))
    missing = [p for p in REQUIRED if not (root / p).exists()]
    status = git(root, 'status', '--porcelain')
    try:
        disk = shutil.disk_usage(root).free
    except OSError:
        disk = None
    return {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'state': 'READY_FOR_AUDIT' if head and origin_matches and not missing else 'BLOCKED_REPOSITORY',
        'head': head, 'audited_baseline': BASELINE, 'head_matches_audited_baseline': head == BASELINE,
        'origin_matches_expected_repository': origin_matches,
        'worktree_has_changes': bool(status), 'missing_required_paths': missing,
        'python': platform.python_version(), 'platform': platform.system(), 'free_disk_bytes': disk,
        'provider_credentials': {n: secret_presence(n) for n in
            ('OPENROUTER_API_KEY', 'ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'HF_TOKEN')},
        'sec_user_agent_present': bool(os.getenv('SEC_USER_AGENT')),
        'network_checks_performed': False, 'real_money_execution_authorized': False,
        'warning': 'Presence is not authentication, entitlement, live data or release qualification. Preserve worktree changes.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    data = inspect(args.repo)
    print(json.dumps(data, indent=2))
    return 0 if data['state'] == 'READY_FOR_AUDIT' else 2

if __name__ == '__main__':
    sys.exit(main())
