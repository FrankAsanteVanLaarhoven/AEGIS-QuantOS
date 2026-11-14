#!/usr/bin/env python3
"""Validate delivery evidence inventory. Does not independently prove test truth.

State shape: {"commit": "<40-char SHA>", "gates": {"gate_id":
{"path": "runtime/.../receipt.json", "sha256": "<64-char hex>"}}}.
Receipt shape: {"gate_id": "...", "commit": "...", "status": "PASS",
"evidence_kind": "local|external|real_model|native_browser", "exit_code": 0,
"command": "<redacted actual command>", "observed_at": "<ISO timestamp>"}.
Include actual raw outputs/provenance alongside receipts. A fabricated receipt
is not validated evidence merely because its checksum is correct.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def evaluate(plan, state, root):
    root=Path(root).resolve()
    commit=state.get('commit','')
    valid_commit=bool(re.fullmatch(r'[0-9a-f]{40}',commit))
    results=[]
    for task in plan['tasks']:
        for required in task['gates']:
            gid=required['id'];entry=state.get('gates',{}).get(gid)
            reason=None
            if not entry:reason='MISSING_EVIDENCE'
            elif not valid_commit:reason='INVALID_TARGET_COMMIT'
            else:
                try:
                    relative=Path(entry['path'])
                    p=(root/relative).resolve()
                    if relative.is_absolute() or not p.is_relative_to(root):
                        raise ValueError('UNSAFE_EVIDENCE_PATH')
                    if p.stat().st_size>5_000_000:raise ValueError('EVIDENCE_TOO_LARGE')
                    raw=p.read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                        raise ValueError('HASH_MISMATCH')
                    receipt=json.loads(raw)
                    if receipt.get('gate_id')!=gid:raise ValueError('GATE_ID_MISMATCH')
                    if receipt.get('commit')!=commit:raise ValueError('STALE_COMMIT_EVIDENCE')
                    if receipt.get('status')!='PASS' or type(receipt.get('exit_code')) is not int or receipt['exit_code']!=0:
                        raise ValueError('GATE_NOT_PASSED')
                    if receipt.get('evidence_kind')!=required['evidence_kind']:
                        raise ValueError('WRONG_EVIDENCE_KIND')
                    if not receipt.get('command') or not receipt.get('observed_at'):
                        raise ValueError('MISSING_EXECUTION_METADATA')
                except (OSError,KeyError,TypeError,json.JSONDecodeError):reason='INVALID_OR_MISSING_RECEIPT'
                except ValueError as exc:reason=str(exc)
            results.append({'task':task['id'],'gate':gid,'state':'PASS' if reason is None else 'BLOCKED','reason':reason})
    passed=sum(x['state']=='PASS' for x in results)
    return {'state':'EVIDENCE_INVENTORY_COMPLETE' if results and passed==len(results) else 'INCOMPLETE',
            'passed':passed,'required':len(results),'gates':results,
            'limitation':'Checks receipt structure, commit binding, types and hashes only. Independently inspect raw executions/provider evidence before qualification.'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',type=Path,default=Path.cwd())
    p.add_argument('--plan',type=Path,default=Path('configs/autonomous_delivery.json'))
    p.add_argument('--state',type=Path,default=Path('runtime/implementation/state.json'))
    a=p.parse_args(argv)
    try:
        plan=json.loads((a.repo/a.plan).read_text())
        path=a.repo/a.state;state=json.loads(path.read_text()) if path.exists() else {}
        result=evaluate(plan,state,a.repo)
        print(json.dumps(result,indent=2))
        return 0 if result['state']=='EVIDENCE_INVENTORY_COMPLETE' else 2
    except (OSError,ValueError,KeyError):
        print(json.dumps({'state':'INVALID_INPUT'}));return 2

if __name__=='__main__':sys.exit(main())
