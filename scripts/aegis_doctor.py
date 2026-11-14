#!/usr/bin/env python3
"""Redacted startup and integration doctor CLI for AEGIS QuantOS."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aegis.doctor import run_doctor
from aegis.settings import build_runtime_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', action='store_true', help='Perform read-only provider auth checks')
    args = parser.parse_args(argv)
    settings, resolver = build_runtime_context()
    report = run_doctor(settings, resolver, check_network=args.network)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
