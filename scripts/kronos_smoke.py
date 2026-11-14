#!/usr/bin/env python3
"""Exercise real pretrained artifacts, never a model double."""
from pathlib import Path
import json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from aegis.data import synthetic
from aegis.kronos_adapter import forecast,manifest_path
result=forecast(synthetic(),59,'NVDA',horizon=4,samples=2,seed=26)
assert result['status']=='REAL_PRETRAINED_KRONOS'
assert len(result['paths'])==2 and all(len(p)==4 for p in result['paths'])
assert all(b['low']<=b['median']<=b['high'] for b in result['bands'])
manifest=json.loads(manifest_path().read_text());manifest['smoke_passed']=True;manifest['smoke_output_valid']=result['ohlcv_violations']==0
manifest_path().write_text(json.dumps(manifest,indent=2))
(ROOT/'evidence'/'kronos_pretrained_smoke.json').write_text(json.dumps(result,indent=2))
print(json.dumps({'passed':True,'latency_seconds':result['latency_seconds'],'ohlcv_violations':result['ohlcv_violations'],'mode':'REAL_PRETRAINED_KRONOS'},indent=2))
