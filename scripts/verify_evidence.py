#!/usr/bin/env python3
"""Independently check a JSON evidence export and its supplied anchor."""
import hashlib,json,sys
from pathlib import Path
report=json.loads(Path(sys.argv[1]).read_text());prev='0'*64;seq=0
for event in report['events']:
    seq+=1;body={k:v for k,v in event.items() if k!='hash'}
    expected=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if event['seq']!=seq or event['prev']!=prev or event['hash']!=expected:raise SystemExit(f'FAIL: event {seq}')
    prev=expected
if report['anchor']['events']!=seq or report['anchor']['head']!=prev:raise SystemExit('FAIL: anchor mismatch')
print(f'PASS: {seq} events; head={prev}')
print('An independently retained anchor is required to establish authenticity against a database administrator.')
