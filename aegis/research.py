"""Cost-aware chronological replay. No optimiser touches the held-out test window."""
from __future__ import annotations
import math, statistics
from .engine import Service, money, D
from .quant import signal, mean, std, bootstrap
from .data import digest

def run_segment(dataset,start,end,strategy,policy,capital=100000):
    engine=Service(':memory:'); engine.reset(dataset,capital)
    with engine.store.tx():
        s=engine.s(); s.update(index=start,strategy=strategy,auto_orders=True,day=dataset.time(start)[:10],equity_history=[{'time':dataset.time(start),'equity':capital}]); s['policy'].update(policy); engine.store.put('state',s)
    for _ in range(start,end): engine.advance()
    snap=engine.snapshot(); history=snap['equity_history']; equities=[x['equity'] for x in history]
    returns=[b/a-1 for a,b in zip(equities,equities[1:])]; deviation=std(returns)
    fills=[o for o in snap['orders'] if o['filled_qty']>0]
    initial=dataset.row(dataset.symbols[0],start)['close']; benchmark=[capital*dataset.row(dataset.symbols[0],i)['close']/initial for i in range(start,end+1)]
    peak=equities[0]; maxdd=0
    for eq in equities: peak=max(peak,eq); maxdd=max(maxdd,1-eq/peak)
    fees=sum(o.get('fee',0) for o in fills); turnover=sum(o['filled_qty']*o['fill_price'] for o in fills)/capital
    body={'strategy':strategy,'window':{'start_index':start,'end_index':end,'start':dataset.time(start),'end':dataset.time(end)},'initial_capital':capital,'final_equity':equities[-1],'net_return':equities[-1]/capital-1,'benchmark_return':benchmark[-1]/capital-1,'max_drawdown':maxdd,'per_bar_sharpe':mean(returns)/deviation if deviation else None,'annualized_sharpe':None,'fills':len(fills),'rejections':sum(o['status']=='REJECTED' for o in snap['orders']),'fees':fees,'turnover':turnover,'curve':[dict(x,benchmark=b) for x,b in zip(history,benchmark)],'orders':fills,'ledger_valid':engine.store.verify()['valid'],'costs':snap['policy'],'benchmark':dataset.symbols[0]+' buy-and-hold; frictionless and not exposure-matched'}
    engine.store.db.close(); return body

def backtest(dataset,strategy='ensemble',cost_bps=6.,walk_forward=True):
    if strategy not in ('ensemble','trend','reversion'): raise ValueError('Backtest strategy must be a deterministic baseline')
    if not math.isfinite(cost_bps) or not 0<=cost_bps<=100: raise ValueError('Invalid transaction costs')
    if dataset.length<100: raise ValueError('At least 100 bars required for a test window')
    split=max(60,int(dataset.length*.65)); split=min(split,dataset.length-25)
    policy={'commission_bps':cost_bps/6,'half_spread_bps':cost_bps/3,'slippage_bps':cost_bps/2}
    result=run_segment(dataset,split,dataset.length-1,strategy,policy)
    folds=[]
    if walk_forward:
        size=max(10,(dataset.length-60)//3)
        for k in range(3):
            start=59+k*size; end=min(dataset.length-1,start+size)
            if end>start:
                f=run_segment(dataset,start,end,strategy,policy); folds.append({key:f[key] for key in ['window','net_return','max_drawdown','fills','per_bar_sharpe']})
    result.update(dataset=dataset.manifest(),protocol={'test_start_fraction':.65,'selection':'Fixed strategy and parameters; no test-set tuning','execution':'Decision after available_at; adverse next-open IOC fills with participation limits','cashflows':'Fees charged both sides; unrealized positions marked at final close, not forcibly liquidated','corporate_actions':'Not handled; input explicitly rejects action events','fold_interpretation':'Rolling fixed-strategy evaluations, not nested model-training cross-validation','benchmark_limitations':'Single-asset frictionless comparator is descriptive, not proof of alpha'},folds=folds,performance_claim='NONE',run_hash=digest({'dataset':dataset.id,'strategy':strategy,'policy':policy,'start':split,'end':dataset.length-1}))
    return result
