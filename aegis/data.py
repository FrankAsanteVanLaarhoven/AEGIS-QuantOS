"""Point-in-time, finite-value OHLCV validation and explicitly synthetic fixtures."""
from __future__ import annotations
import csv, hashlib, io, json, math, random, re
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
SYMBOLS = {'NVDA':'NVIDIA', 'AMAT':'Applied Materials', 'LRCX':'Lam Research', 'TER':'Teradyne', 'SPY':'S&P 500 ETF'}

def iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace('+00:00','Z')

def dt(s: str) -> datetime:
    value=datetime.fromisoformat(s.replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('Timezone-aware timestamps required')
    return value.astimezone(UTC)

def canonical(x) -> str:
    return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(x) -> str:
    return hashlib.sha256(canonical(x).encode()).hexdigest()

class Dataset:
    def __init__(self, rows: list[dict], label: str='Uploaded research dataset', source: str='USER_UPLOAD', raw_hash: str=''):
        if not rows or len(rows)>50000: raise ValueError('Dataset must contain 1–50,000 rows')
        self.label=label[:120]; self.source=source; self.groups={}; seen=set()
        for row in rows:
            r=dict(row); sym=str(r.get('symbol','')).upper()
            if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,11}',sym): raise ValueError('Invalid symbol')
            r['symbol']=sym
            for key in ['open','high','low','close','volume']:
                try: v=float(r[key])
                except (KeyError,ValueError,TypeError): raise ValueError(f'Invalid {key}')
                if not math.isfinite(v) or v<0 or (key!='volume' and v==0): raise ValueError(f'Nonfinite or nonpositive {key}')
                r[key]=v
            if r['volume'] != int(r['volume']): raise ValueError('Volume must be an integer')
            if r['high']<max(r['open'],r['close'],r['low']) or r['low']>min(r['open'],r['close'],r['high']): raise ValueError('Invalid OHLC envelope')
            end=dt(r['timestamp']); available=dt(r.get('available_at',r['timestamp']))
            start=dt(r.get('open_at',iso(end-timedelta(seconds=299))))
            if start>=end or available<end: raise ValueError('Invalid availability chronology')
            r.update(timestamp=iso(end),available_at=iso(available),open_at=iso(start))
            # Corporate action support is deliberately not implicit.
            if float(r.get('split_factor',1))!=1 or float(r.get('dividend',0))!=0:
                raise ValueError('Corporate-action events are not supported; use a documented split-consistent research snapshot')
            key=(sym,r['timestamp'])
            if key in seen: raise ValueError('Duplicate symbol/timestamp')
            seen.add(key); self.groups.setdefault(sym,[]).append(r)
        if len(self.groups)>20: raise ValueError('At most 20 symbols')
        self.symbols=list(self.groups)
        reference=None
        for sym, bars in self.groups.items():
            bars.sort(key=lambda r:r['timestamp'])
            if len(bars)<65: raise ValueError('At least 65 aligned bars per symbol required')
            keys=[(r['timestamp'],r['open_at'],r['available_at']) for r in bars]
            if reference is not None and keys!=reference: raise ValueError('All symbols must have identical aligned timestamps and availability')
            reference=keys
            for a,b in zip(bars,bars[1:]):
                if dt(b['open_at'])<dt(a['available_at']): raise ValueError('Next open must not precede prior data availability')
        self.length=len(next(iter(self.groups.values())))
        self.id=digest({'source':source,'rows':[r for s in sorted(self.groups) for r in self.groups[s]]})[:20]
        self.raw_hash=raw_hash or digest({'source':source,'rows':[r for s in sorted(self.groups) for r in self.groups[s]]})
    def prefix(self,symbol: str,index: int) -> list[dict]:
        if symbol not in self.groups: raise ValueError('Unknown symbol')
        return self.groups[symbol][:index+1]
    def row(self,symbol: str,index: int) -> dict: return self.groups[symbol][index]
    def time(self,index: int) -> str: return self.row(self.symbols[0],index)['available_at']
    def manifest(self) -> dict:
        return {'id':self.id,'label':self.label,'source':self.source,'rows':self.length*len(self.symbols),'bars_per_symbol':self.length,'symbols':self.symbols,'sha256':self.raw_hash,'start':self.time(0),'end':self.time(self.length-1),'price_basis':'research snapshot; corporate actions not processed','survivorship_control':'not established','historical_latency':'availability must be supplied honestly by uploader'}
    def to_rows(self) -> list[dict]: return [r for s in self.symbols for r in self.groups[s]]
    def csv(self) -> str:
        f=io.StringIO(); cols=['symbol','open_at','timestamp','available_at','open','high','low','close','volume']
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore'); w.writeheader(); w.writerows(self.to_rows()); return f.getvalue()

def synthetic(seed: int=26, sessions: int=6, bars_per_session: int=60) -> Dataset:
    rng=random.Random(seed); rows=[]; prices={'NVDA':128.,'AMAT':176.,'LRCX':82.,'TER':102.,'SPY':548.}
    # These are seeded examples, not actual historic prices or exchange records.
    start=datetime(2026,11,2,14,30,tzinfo=UTC)
    for i in range(sessions*bars_per_session):
        day=i//bars_per_session; day+=2 if day>=5 else 0
        t=start+timedelta(days=day,minutes=5*(i%bars_per_session))
        market=rng.gauss(0,0.0009)+0.00020*math.sin(i/29)
        for k,sym in enumerate(SYMBOLS):
            old=prices[sym]; trend=0.00012+0.00045*math.sin(i/18+k*.7)
            op=old*(1+rng.gauss(0,.0003)); close=op*(1+market*(.6 if sym=='SPY' else 1.2)+rng.gauss(0,.0013)+trend)
            spread=abs(rng.gauss(.0009,.0005)); high=max(op,close)*(1+spread); low=min(op,close)*(1-spread)
            rows.append({'symbol':sym,'open_at':iso(t),'timestamp':iso(t+timedelta(seconds=299)),'available_at':iso(t+timedelta(seconds=300)), 'open':round(op,4),'high':round(high,4),'low':round(low,4),'close':round(close,4),'volume':int(rng.uniform(12000,140000)*(1.5 if sym=='NVDA' else 1))})
            prices[sym]=close
    return Dataset(rows,'Semiconductor research basket · seeded fixture','SYNTHETIC')

def from_csv(text: str,label: str) -> Dataset:
    if len(text.encode())>8_000_000: raise ValueError('CSV limit: 8 MB')
    reader=csv.DictReader(io.StringIO(text))
    required={'symbol','timestamp','available_at','open','high','low','close','volume'}
    if not required.issubset(reader.fieldnames or []): raise ValueError('CSV missing required headers: '+', '.join(sorted(required)))
    return Dataset(list(reader),label,'USER_UPLOAD',hashlib.sha256(text.encode()).hexdigest())
