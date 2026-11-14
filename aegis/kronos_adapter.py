"""Adapter to the REAL pinned upstream implementation. No substitute predictions.
The reviewed upstream averages sample_count paths. We explicitly retain independent
single-sample calls so quantiles are computed across trajectories, not averaged paths.
"""
from __future__ import annotations
import hashlib, importlib, json, math, os, sys, threading, time
from pathlib import Path
from .data import dt, digest
from .market.calendar import forward_bar_timestamps

COMMIT='67b630e67f6a18c9e9be918d9b4337c960db1e9a'
ROOT=Path(__file__).resolve().parents[1]
LOCK=threading.RLock()

class ModelUnavailable(RuntimeError): pass

def manifest_path(): return Path(os.environ.get('AEGIS_MODELS_DIR',str(ROOT/'models')))/'kronos.lock.json'
def status():
    p=manifest_path()
    if not p.exists(): return {'state':'NOT_INSTALLED','model':'Kronos-mini','source_commit':COMMIT,'reason':'Run python scripts/install_kronos.py. No weights are bundled and no surrogate forecasts are produced.'}
    try:
        m=json.loads(p.read_text()); return {'state':'INSTALLED_UNVERIFIED' if not m.get('smoke_passed') else 'READY','model':m['model_repo'],'source_commit':m['source_commit'],'device':os.environ.get('AEGIS_MODEL_DEVICE','cpu'),'manifest_hash':hashlib.sha256(p.read_bytes()).hexdigest()}
    except Exception as exc: return {'state':'INVALID_MANIFEST','reason':str(exc)}

def verify_artifacts(m):
    base=manifest_path().parent.resolve(); source=(ROOT/'vendor'/'Kronos').resolve()
    if m.get('source_commit')!=COMMIT: raise ModelUnavailable('Unexpected upstream commit')
    for area,root in [('source',source),('weights',base)]:
        entries=m.get(area,{})
        if not entries: raise ModelUnavailable('Artifact checksums missing')
        for relative,h in entries.items():
            p=(root/relative).resolve()
            if not p.is_relative_to(root) or not p.is_file(): raise ModelUnavailable('Artifact missing or unsafe path')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=h: raise ModelUnavailable('Artifact digest mismatch: '+relative)
    for folder in ('model','tokenizer'):
        if not (base/folder/'model.safetensors').exists(): raise ModelUnavailable('Safe tensor weights missing')
    return base,source

_cached=None

def load():
    global _cached
    p=manifest_path()
    if not p.exists(): raise ModelUnavailable(status()['reason'])
    raw=p.read_bytes(); key=hashlib.sha256(raw).hexdigest(); m=json.loads(raw)
    with LOCK:
        base,source=verify_artifacts(m)
        if _cached and _cached[0]==key: return _cached[1],m
        try:
            import torch
            from safetensors.torch import load_file
            sys.path.insert(0,str(source))
            module=importlib.import_module('model')
            if not Path(module.__file__).resolve().is_relative_to(source): raise ModelUnavailable('Unexpected model module import location')
            # Avoid pickle fallback and network calls during inference.
            tok=module.KronosTokenizer(**json.loads((base/'tokenizer'/'config.json').read_text()))
            model=module.Kronos(**json.loads((base/'model'/'config.json').read_text()))
            tok.load_state_dict(load_file(str(base/'tokenizer'/'model.safetensors')),strict=True)
            model.load_state_dict(load_file(str(base/'model'/'model.safetensors')),strict=True)
            model.eval(); tok.eval()
            device=os.environ.get('AEGIS_MODEL_DEVICE','cpu')
            predictor=module.KronosPredictor(model,tok,device=device,max_context=m['max_context'])
            _cached=(key,predictor); return predictor,m
        except Exception as exc: raise ModelUnavailable('Kronos load failed: '+str(exc)) from exc

def forecast(dataset,index,symbol,horizon=12,samples=4,seed=26,use_exchange_calendar=False):
    if symbol not in dataset.symbols or not 1<=horizon<=60 or not 2<=samples<=16: raise ValueError('Invalid forecast bounds')
    bars=dataset.prefix(symbol,index)
    if use_exchange_calendar:
        future_ts=[ts.isoformat() for ts in forward_bar_timestamps(dt(bars[-1]['timestamp']),horizon,bar_minutes=15)]
    elif index+horizon>=dataset.length:
        raise ValueError('Not enough declared future calendar timestamps; shorten horizon or pass use_exchange_calendar=True')
    else:
        future_ts=[dataset.row(symbol,i)['timestamp'] for i in range(index+1,index+horizon+1)]
    with LOCK:
        predictor,m=load()
        import numpy as np, pandas as pd, torch
        bars=bars[-m['max_context']:]; frame=pd.DataFrame([{k:b[k] for k in ('open','high','low','close','volume')} for b in bars])
        # Upstream imputes traded amount as volume * mean(OHLC); disclose it.
        x=pd.Series(pd.to_datetime([b['timestamp'] for b in bars],utc=True))
        y=pd.Series(pd.to_datetime(future_ts,utc=True))
        raw=[]; begun=time.perf_counter()
        with torch.random.fork_rng(devices=list(range(torch.cuda.device_count())) if torch.cuda.is_available() else []):
            with torch.inference_mode():
                for j in range(samples):
                    torch.manual_seed(seed+j)
                    p=predictor.predict(frame,x,y,pred_len=horizon,T=1.,top_k=0,top_p=.9,sample_count=1,verbose=False)
                    a=p[['open','high','low','close','volume','amount']].to_numpy(dtype=float)
                    if not np.isfinite(a).all(): raise ModelUnavailable('Nonfinite model output rejected')
                    raw.append(a)
        a=np.stack(raw); violations=int(((a[:,:,0]<=0)|(a[:,:,1]<np.maximum(a[:,:,0],a[:,:,3]))|(a[:,:,2]>np.minimum(a[:,:,0],a[:,:,3]))|(a[:,:,2]<=0)|(a[:,:,4]<0)|(a[:,:,5]<0)).sum())
        prices=a[:,:,3]; bands=[{'step':i+1,'timestamp':y.iloc[i].isoformat(),'low':float(np.quantile(prices[:,i],.05)),'median':float(np.median(prices[:,i])),'high':float(np.quantile(prices[:,i],.95))} for i in range(horizon)]
        return {'model':m['model_repo'],'source_commit':COMMIT,'model_revision':m['model_revision'],'tokenizer_revision':m['tokenizer_revision'],'dataset_id':dataset.id,'asof':dataset.time(index),'symbol':symbol,'horizon':horizon,'samples':samples,'seed':seed,'bands':bands,'paths':prices.tolist(),'latency_seconds':time.perf_counter()-begun,'input_hash':digest(bars),'ohlcv_violations':violations,'ohlcv_valid':violations==0,'execution_eligible':False,'calibrated':False,'amount_source':'Upstream proxy: volume × mean(open, high, low, close)','future_information':'Calendar timestamps only; no future OHLCV supplied','status':'REAL_PRETRAINED_KRONOS','limitation':'Empirical quantiles from a small sample, not calibrated prediction intervals. Reproducibility is within the same software/hardware, not bit-exact across GPUs.'}
