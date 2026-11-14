#!/usr/bin/env python3
"""Start a single-operator, loopback-only paper workspace."""
from pathlib import Path
import os, secrets, sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
if (ROOT/'.env').exists():
    for line in (ROOT/'.env').read_text().splitlines():
        line=line.strip()
        if line and not line.startswith('#') and '=' in line:
            k,v=line.split('=',1);os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
RUNTIME=ROOT/'runtime';RUNTIME.mkdir(exist_ok=True);p=RUNTIME/'access.token'
if not os.environ.get('AEGIS_TOKEN'):
    if not p.exists():
        fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    os.environ['AEGIS_TOKEN']=p.read_text().strip()
port=int(os.environ.get('AEGIS_PORT','8765'))
print('\nAEGIS QuantOS 0.2 — local paper-trading workspace',flush=True)
print(f'Open: http://127.0.0.1:{port}',flush=True)
print('Workspace token: '+os.environ['AEGIS_TOKEN'],flush=True)
print('No live execution. Use Ctrl+C to stop.\n',flush=True)
import uvicorn
from aegis.api import create_app
uvicorn.run(create_app(),host='127.0.0.1',port=port,workers=1,log_level='warning')
