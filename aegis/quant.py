"""Deterministic research calculations. No LLM, calibrated odds or trading advice implied."""
from __future__ import annotations
import math, random, statistics
from .data import digest

def mean(xs): return sum(xs)/len(xs) if xs else 0.
def std(xs): return statistics.stdev(xs) if len(xs)>1 else 0.
def percentile(values,p):
    v=sorted(values); k=(len(v)-1)*p; a=math.floor(k); b=math.ceil(k)
    return v[a]+(v[b]-v[a])*(k-a)
def signal(bars, strategy='ensemble'):
    if len(bars)<25: return {'action':'HOLD','score':0.,'reason':'Warm-up: at least 25 observations','momentum':0.,'reversion':0.,'volatility':0.,'sma_fast':0.,'sma_slow':0.}
    p=[b['close'] for b in bars]; history=p[-31:]; ret=[math.log(b/a) for a,b in zip(history,history[1:])]; vol=std(ret)
    fast=mean(p[-8:]); slow=mean(p[-21:]); momentum=math.tanh((fast/slow-1)/max(vol*2,0.001))
    z=(p[-1]-mean(p[-20:]))/max(std(p[-20:]),1e-6); rev=-math.tanh(z/2)
    score={'trend':momentum,'reversion':rev,'ensemble':.7*momentum+.3*rev}[strategy]
    action='BUY' if score>.13 else ('SELL' if score<-.13 else 'HOLD')
    return {'action':action,'score':round(score,6),'momentum':round(momentum,6),'reversion':round(rev,6),'volatility':round(vol,6),'sma_fast':fast,'sma_slow':slow,'reason':f'{strategy}: score {score:+.3f}; deadband ±0.13. Score is NOT a probability.'}

def scan(dataset,index,strategy='ensemble'):
    out=[]
    for sym in dataset.symbols:
        bars=dataset.prefix(sym,index); last=bars[-1]; prev=bars[-2]; s=signal(bars,strategy)
        out.append({'symbol':sym,'price':last['close'],'change_pct':100*(last['close']/prev['close']-1),'volume':last['volume'],'relative_volume':last['volume']/max(1,mean([b['volume'] for b in bars[-21:-1]])),'timestamp':last['available_at'],'signal':s,'setup':'Trend continuation' if s['momentum']>.3 else ('Mean-reversion candidate' if s['reversion']>.3 else 'No confirmed setup'),'series':[b['close'] for b in bars[-24:]]})
    return out

def bootstrap(bars,horizon=30,trials=1500,seed=26,stop_pct=.02,target_pct=.03,block=5,cost_bps=7):
    if not 2<=horizon<=120 or not 100<=trials<=10000: raise ValueError('Simulation bounds exceeded')
    if not 0<stop_pct<.5 or not 0<target_pct<1: raise ValueError('Invalid scenario levels')
    close=[b['close'] for b in bars]; returns=[math.log(b/a) for a,b in zip(close,close[1:])]
    if len(returns)<30: raise ValueError('Insufficient history')
    rng=random.Random(seed); paths=[]; terminal=[]; wins=losses=unresolved=0
    block=min(block,len(returns)); start=close[-1]; charge=cost_bps/10000
    for trial in range(trials):
        path=[start]; sampled=[]
        while len(sampled)<horizon:
            j=rng.randrange(len(returns)-block+1); sampled.extend(returns[j:j+block])
        hit=None
        for r in sampled[:horizon]:
            path.append(path[-1]*math.exp(r))
            if hit is None:
                if path[-1]<=start*(1-stop_pct): hit='stop'
                elif path[-1]>=start*(1+target_pct): hit='target'
        wins+=hit=='target'; losses+=hit=='stop'; unresolved+=hit is None
        terminal.append(path[-1]/start-1-charge); paths.append(path)
    bands=[{'step':j,'low':percentile([p[j] for p in paths],.05),'median':percentile([p[j] for p in paths],.5),'high':percentile([p[j] for p in paths],.95)} for j in range(horizon+1)]
    tail=sorted(terminal)[:max(1,math.ceil(.05*trials))]
    return {'method':'Contiguous moving-block bootstrap','seed':seed,'trials':trials,'horizon':horizon,'block_size':block,'history_bars':len(bars),'start_price':start,'bands':bands,'paths':paths[:20],'mean_net_return':mean(terminal),'p05_net_return':percentile(terminal,.05),'p95_net_return':percentile(terminal,.95),'expected_shortfall_loss':-mean(tail),'target_first':wins/trials,'stop_first':losses/trials,'unresolved':unresolved/trials,'calibrated':False,'limitations':'Empirical scenario frequencies, not calibrated predictive probabilities. Close-only barrier observations; no jumps or unseen regimes guaranteed. No execution matching.'}

def correlation(dataset,index):
    series={}
    for s in dataset.symbols:
        h=dataset.prefix(s,index)[-61:]
        series[s]=[math.log(b['close']/a['close']) for a,b in zip(h,h[1:])]
    matrix=[]
    for a in dataset.symbols:
        row=[]
        for b in dataset.symbols:
            x=series[a]; y=series[b]; mx=mean(x); my=mean(y)
            den=math.sqrt(sum((i-mx)**2 for i in x)*sum((i-my)**2 for i in y))
            row.append(sum((i-mx)*(j-my) for i,j in zip(x,y))/den if den else (1 if a==b else 0))
        matrix.append(row)
    return {'symbols':dataset.symbols,'matrix':matrix,'observations':min(map(len,series.values()))}

def valuation(fcf,growth,discount,terminal_growth,years,shares,debt=0,cash=0):
    values=[fcf,growth,discount,terminal_growth,shares,debt,cash]
    if any(not math.isfinite(float(x)) for x in values): raise ValueError('Nonfinite valuation input')
    if fcf<=0 or shares<=0 or not 1<=years<=15 or discount<=terminal_growth or discount<=0 or growth<=-1 or terminal_growth<=-1 or debt<0 or cash<0: raise ValueError('Invalid DCF assumptions')
    flows=[fcf*(1+growth)**i for i in range(1,years+1)]
    pv=sum(v/(1+discount)**(i+1) for i,v in enumerate(flows)); terminal=flows[-1]*(1+terminal_growth)/(discount-terminal_growth)/(1+discount)**years
    enterprise=pv+terminal; equity=enterprise-debt+cash
    return {'enterprise_value':enterprise,'equity_value':equity,'per_share':equity/shares,'cashflows':flows,'terminal_share':terminal/enterprise,'assumptions':{'fcf':fcf,'growth':growth,'discount':discount,'terminal_growth':terminal_growth,'years':years,'shares':shares,'debt':debt,'cash':cash},'status':'USER_ASSUMPTIONS_NOT_VERIFIED_FINANCIALS'}
