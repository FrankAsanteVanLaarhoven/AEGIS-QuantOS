"""Long-only research simulator; risk checks and fills share an atomic transaction.
Next-bar-open fills, adverse spread/slippage, commission and IOC participation cap.
Not a live broker, FTMO rule certification, or order-book matching engine.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from copy import deepcopy
from datetime import datetime
import math, uuid
from .data import synthetic, digest, dt
from .quant import scan, signal, correlation
from .store import Store

def D(x): return Decimal(str(x))
def money(x): return str(D(x).quantize(D('.000001'),rounding=ROUND_HALF_UP))
def uid(): return uuid.uuid4().hex[:16]
DEFAULT_POLICY={'max_order_notional':10000.,'max_position_pct':.25,'max_gross_pct':.80,'max_daily_loss_pct':.03,'max_drawdown_pct':.08,'commission_bps':1.,'half_spread_bps':2.,'slippage_bps':3.,'max_participation':.01,'max_age_seconds':600,'reset_timezone':'UTC'}
STRATEGIES={'trend','reversion','ensemble'}

class Service:
    def __init__(self,path=':memory:'):
        self.store=Store(path)
        with self.store.tx():
            if self.store.get('state') is None:
                d=synthetic(); self.store.save_dataset(d); self.store.put('state',self.fresh(d)); self.store.append('system.initialized',{'mode':'PAPER_REPLAY','dataset':d.manifest(),'scope':'v5','build':'0.2.0'})
        self.dataset=self.store.load_dataset(self.store.get('state')['dataset_id'])
    def fresh(self,d,capital=100000):
        return {'dataset_id':d.id,'index':59,'initial_cash':money(capital),'cash':money(capital),'positions':{},'orders':[],'halted':False,'halt_reason':None,'policy':deepcopy(DEFAULT_POLICY),'strategy':'ensemble','auto_orders':False,'running':False,'interval_seconds':2.,'step_count':0,'day':d.time(59)[:10],'day_open_equity':money(capital),'peak_equity':money(capital),'equity_history':[{'time':d.time(59),'equity':capital}],'alerts':[],'trace':[]}
    def s(self): return self.store.get('state')
    def equity(self,s,marks=None):
        marks=marks or {x:D(self.dataset.row(x,s['index'])['close']) for x in self.dataset.symbols}
        return D(s['cash'])+sum((D(p['qty'])*D(marks[x]) for x,p in s['positions'].items()),D(0))
    def stats(self,s,marks=None):
        marks=marks or {x:D(self.dataset.row(x,s['index'])['close']) for x in self.dataset.symbols}
        eq=self.equity(s,marks); gross=sum((D(p['qty'])*D(marks[x]) for x,p in s['positions'].items()),D(0))
        peak=max(eq,D(s['peak_equity'])); daily=max(D(0),(D(s['day_open_equity'])-eq)/D(s['day_open_equity'])); dd=max(D(0),(peak-eq)/peak)
        return {'equity':float(eq),'cash':float(D(s['cash'])),'gross':float(gross),'gross_pct':float(gross/eq) if eq>0 else 1.,'pnl':float(eq-D(s['initial_cash'])),'return_pct':float((eq/D(s['initial_cash'])-1)*100),'drawdown_pct':float(dd),'daily_loss_pct':float(daily)}
    def snapshot(self):
        with self.store.lock:
            s=self.s(); stats=self.stats(s); positions=[]
            for sym,p in s['positions'].items():
                mark=D(self.dataset.row(sym,s['index'])['close']); qty=D(p['qty']); avg=D(p['average'])
                positions.append({'symbol':sym,'qty':int(qty),'average':float(avg),'mark':float(mark),'value':float(qty*mark),'unrealized':float(qty*(mark-avg))})
            return {**s,'positions':positions,'stats':stats,'dataset':self.dataset.manifest(),'asof':self.dataset.time(s['index']),'remaining_bars':self.dataset.length-1-s['index'],'scanner':scan(self.dataset,s['index'],s['strategy'] if s['strategy'] in STRATEGIES else 'ensemble'),'mode':'PAPER_REPLAY','live_enabled':False,'audit':self.store.verify()}
    def _check(self,s,sym,side,qty,price,exclude=None,marks=None):
        p=s['policy']; errors=[]; marks=marks or {x:D(self.dataset.row(x,s['index'])['close']) for x in self.dataset.symbols}
        pending=[o for o in s['orders'] if o['status']=='QUEUED' and o['id']!=exclude]
        cost=price*D(qty); fees=cost*D(p['commission_bps'])/D(10000)
        if sym not in self.dataset.symbols: return ['UNKNOWN_SYMBOL']
        if side not in ['BUY','SELL']: return ['INVALID_SIDE']
        if not isinstance(qty,int) or isinstance(qty,bool) or qty<=0: return ['INVALID_QUANTITY']
        held=int(s['positions'].get(sym,{}).get('qty',0))
        if side=='SELL':
            reserved=sum(o['qty'] for o in pending if o['side']=='SELL' and o['symbol']==sym)
            if qty>held-reserved: errors.append('SHORT_SELLING_DISABLED')
            return errors # Risk-reducing sells remain possible during a halt.
        if s['halted']: errors.append('TRADING_HALTED')
        st=self.stats(s,marks); eq=D(st['equity'])
        if eq<=0: errors.append('NONPOSITIVE_EQUITY'); return errors
        if st['daily_loss_pct']>=p['max_daily_loss_pct']: errors.append('DAILY_LOSS_LIMIT')
        if st['drawdown_pct']>=p['max_drawdown_pct']: errors.append('DRAWDOWN_LIMIT')
        if cost>D(p['max_order_notional']): errors.append('ORDER_NOTIONAL_LIMIT')
        reserved_cash=sum((D(o['reserved']) for o in pending if o['side']=='BUY'),D(0))
        if cost+fees+reserved_cash>D(s['cash']): errors.append('INSUFFICIENT_CASH')
        pending_sym=sum(o['qty'] for o in pending if o['side']=='BUY' and o['symbol']==sym)
        if D(held+pending_sym)*D(marks[sym])+cost>eq*D(p['max_position_pct']): errors.append('POSITION_LIMIT')
        if D(st['gross'])+reserved_cash+cost>eq*D(p['max_gross_pct']): errors.append('GROSS_EXPOSURE_LIMIT')
        return errors
    def _submit(self,s,sym,side,qty,client_id,stop=None,target=None,origin='MANUAL'):
        if sym not in self.dataset.symbols: raise ValueError('Unknown symbol')
        if side not in ('BUY','SELL') or not isinstance(qty,int) or isinstance(qty,bool) or not 1<=qty<=100000: raise ValueError('Invalid side or quantity')
        req={'symbol':sym,'side':side,'qty':qty,'stop':stop,'target':target,'origin':origin}; request_hash=digest(req)
        for order in s['orders']:
            if order['client_id']==client_id:
                if order['request_hash']!=request_hash: raise ValueError('Idempotency key conflict')
                return order
        row=self.dataset.row(sym,s['index']); px=D(row['close']); p=s['policy']
        for v in (stop,target):
            if v is not None and (not math.isfinite(v) or v<=0): raise ValueError('Invalid bracket level')
        if side=='SELL' and (stop or target): raise ValueError('Brackets apply to buys only')
        if stop is not None and D(stop)>=px: raise ValueError('Stop must be below current price')
        if target is not None and D(target)<=px: raise ValueError('Target must be above current price')
        estimate=px*(1+D(p['half_spread_bps']+p['slippage_bps'])/10000)
        reasons=self._check(s,sym,side,qty,estimate)
        age=(dt(self.dataset.time(s['index']))-dt(row['available_at'])).total_seconds()
        if age<0 or age>p['max_age_seconds']: reasons.append('INVALID_DATA_FRESHNESS')
        if s['index']>=self.dataset.length-1: reasons.append('END_OF_DATA')
        order={'id':uid(),'client_id':client_id,'request_hash':request_hash,**req,'status':'REJECTED' if reasons else 'QUEUED','reasons':reasons,'submitted_at':self.dataset.time(s['index']),'submitted_index':s['index'],'filled_qty':0,'fill_price':None,'fee':0.,'reserved':money(estimate*qty*(1+D(p['commission_bps'])/10000))}
        s['orders'].append(order); self.store.append('order.'+order['status'].lower(),order)
        return order
    def reconcile(self):
        with self.store.lock:
            s=self.s(); expected_cash=D(s['initial_cash']); quantities={}; fills=0
            for o in s['orders']:
                qty=o.get('filled_qty',0)
                if not qty: continue
                fills+=1; notional=D(o['fill_price'])*qty; fee=D(o.get('fee',0)); sign=1 if o['side']=='BUY' else -1
                expected_cash-=D(sign)*notional+fee
                quantities[o['symbol']]=quantities.get(o['symbol'],0)+sign*qty
            quantities={k:v for k,v in quantities.items() if v}
            actual={k:int(v['qty']) for k,v in s['positions'].items() if v['qty']}
            cash_difference=D(s['cash'])-expected_cash
            valid=abs(cash_difference)<=D('.000001') and quantities==actual
            return {'valid':valid,'filled_orders':fills,'cash_difference':str(cash_difference),'expected_cash':str(expected_cash),'expected_positions':quantities,'actual_positions':actual,'scope':'Current local paper session; broker reconciliation is a separate read-only connector'}

    def _ensure_integrity(self):
        if not self.store.verify()['valid'] or not self.reconcile()['valid']:
            raise ValueError('Evidence or account reconciliation failed; execution blocked')

    def submit(self,symbol,side,qty,client_id=None,stop=None,target=None):
        with self.store.tx():
            s=self.s()
            self._ensure_integrity()
            o=self._submit(s,symbol,side,qty,client_id or uid(),stop,target); self.store.put('state',s); return o
    def _fill(self,s,o,row,raw_price=None,protected=False,liquidity_left=None):
        p=s['policy']; price=D(raw_price if raw_price is not None else row['open']); direction=1 if o['side']=='BUY' else -1
        price=(price*(1+D(direction)*D(p['half_spread_bps']+p['slippage_bps'])/10000)).quantize(D('.000001'))
        cap=int(D(row['volume'])*D(p['max_participation'])); cap=min(cap,liquidity_left) if liquidity_left is not None else cap; qty=min(o['qty'],cap)
        marks={sym:D(self.dataset.row(sym,s['index']+1)['open']) for sym in self.dataset.symbols}
        errors=self._check(s,o['symbol'],o['side'],qty,price,exclude=o['id'],marks=marks) if qty else ['NO_MODELED_LIQUIDITY']
        if o['side']=='BUY' and o['stop'] is not None and price<=D(o['stop']): errors.append('GAP_BELOW_STOP')
        if o['side']=='BUY' and o['target'] is not None and price>=D(o['target']): errors.append('GAP_ABOVE_TARGET')
        if errors:
            o.update(status='REJECTED',reasons=errors); self.store.append('order.rejected_at_fill',o); return 0
        notional=price*qty; fee=(notional*D(p['commission_bps'])/10000).quantize(D('.000001')); sym=o['symbol']; old=s['positions'].get(sym,{'qty':0,'average':'0','stop':None,'target':None}); oldq=int(old['qty'])
        if o['side']=='BUY':
            newq=oldq+qty; average=(D(old['average'])*oldq+notional+fee)/newq
            s['cash']=money(D(s['cash'])-notional-fee); s['positions'][sym]={'qty':newq,'average':money(average),'stop':o['stop'] if o['stop'] is not None else old.get('stop'),'target':o['target'] if o['target'] is not None else old.get('target')}
        else:
            s['cash']=money(D(s['cash'])+notional-fee)
            if oldq==qty: s['positions'].pop(sym,None)
            else: s['positions'][sym]['qty']=oldq-qty
        o.update(status='FILLED' if qty==o['qty'] else 'PARTIALLY_FILLED_CANCELLED',filled_qty=qty,fill_price=float(price),fee=float(fee),filled_at=row['open_at'],remainder_cancelled=o['qty']-qty)
        self.store.append('order.filled',o)
        return qty
    def _cycle(self,s):
        records=scan(self.dataset,s['index'],s['strategy']); trace=[]
        for stage in ['Market data','Signal research','Portfolio sizing','Risk policy','Paper execution','Audit evidence']:
            trace.append({'stage':stage,'status':'COMPLETE','asof':self.dataset.time(s['index']),'kind':'DETERMINISTIC_WORKER'})
        for r in records:
            sym=r['symbol']; action=r['signal']['action']; held=int(s['positions'].get(sym,{}).get('qty',0)); px=D(r['price'])
            if action=='BUY' and held==0:
                budget=min(D(self.stats(s)['equity'])*D('.06'),D(s['policy']['max_order_notional'])*D('.9'))
                qty=int(budget/px)
                if qty: self._submit(s,sym,'BUY',qty,f"auto-{s['dataset_id']}-{s['index']}-{sym}",float(px*D('.98')),float(px*D('1.04')),'AUTO_BASELINE')
            elif action=='SELL' and held:
                if not any(o['status']=='QUEUED' and o['symbol']==sym and o['side']=='SELL' for o in s['orders']): self._submit(s,sym,'SELL',held,f"auto-{s['dataset_id']}-{s['index']}-{sym}",origin='AUTO_BASELINE')
        s['trace']=trace; self.store.append('research.cycle',{'index':s['index'],'strategy':s['strategy'],'signals':records,'trace':trace})
    def cycle(self):
        with self.store.tx():
            self._ensure_integrity()
            s=self.s(); self._cycle(s); self.store.put('state',s)
        return self.snapshot()
    def advance(self,n=1):
        if not isinstance(n,int) or not 1<=n<=100: raise ValueError('Steps must be 1–100')
        with self.store.tx():
            self._ensure_integrity()
            s=self.s()
            for _ in range(n):
                if s['index']>=self.dataset.length-1: s['running']=False; break
                nextday=self.dataset.time(s['index']+1)[:10]
                if nextday!=s['day']:
                    # Start-of-session equity reference is prior close, before overnight gaps.
                    s['day']=nextday; s['day_open_equity']=money(self.equity(s))
                if s['auto_orders'] and not s['halted']: self._cycle(s)
                pending=[o for o in s['orders'] if o['status']=='QUEUED' and o['submitted_index']<=s['index']]
                # Sell reservations are honored before protective exits are considered.
                liquidity={sym:int(D(self.dataset.row(sym,s['index']+1)['volume'])*D(s['policy']['max_participation'])) for sym in self.dataset.symbols}
                for o in sorted(pending,key=lambda o:o['side']=='BUY'):
                    used=self._fill(s,o,self.dataset.row(o['symbol'],s['index']+1),liquidity_left=liquidity[o['symbol']]); liquidity[o['symbol']]-=used
                for sym,pos in list(s['positions'].items()):
                    row=self.dataset.row(sym,s['index']+1); stop=pos.get('stop'); target=pos.get('target'); trigger=None; raw=None
                    if stop and row['low']<=stop: trigger='STOP'; raw=min(row['open'],stop)
                    elif target and row['high']>=target: trigger='TARGET'; raw=target # Conservative no favorable price improvement.
                    if trigger:
                        o={'id':uid(),'client_id':uid(),'request_hash':'PROTECTIVE','symbol':sym,'side':'SELL','qty':int(pos['qty']),'stop':None,'target':None,'origin':trigger,'status':'QUEUED','reasons':[],'submitted_at':row['open_at'],'submitted_index':s['index'],'filled_qty':0,'reserved':'0'}
                        s['orders'].append(o); used=self._fill(s,o,row,raw,True,liquidity_left=liquidity[sym]); liquidity[sym]-=used
                s['index']+=1; s['step_count']+=1; st=self.stats(s)
                s['peak_equity']=money(max(D(s['peak_equity']),D(st['equity'])))
                if st['daily_loss_pct']>=s['policy']['max_daily_loss_pct'] or st['drawdown_pct']>=s['policy']['max_drawdown_pct']:
                    s['halted']=True; s['halt_reason']='ACCOUNT_LOSS_LIMIT'; s['running']=False
                s['equity_history'].append({'time':self.dataset.time(s['index']),'equity':st['equity']})
                for a in s['alerts']:
                    price=self.dataset.row(a['symbol'],s['index'])['close']
                    if not a['triggered'] and ((a['direction']=='above' and price>=a['price']) or (a['direction']=='below' and price<=a['price'])):
                        a.update(triggered=True,triggered_at=self.dataset.time(s['index'])); self.store.append('alert.triggered',a)
                self.store.append('replay.advanced',{'index':s['index'],'asof':self.dataset.time(s['index']),'account':st,'positions':s['positions'],'dataset':self.dataset.id})
            self.store.put('state',s)
        return self.snapshot()
    def halt(self,enabled=True):
        with self.store.tx():
            s=self.s(); s.update(halted=bool(enabled),halt_reason='OPERATOR_HALT' if enabled else None,running=False)
            if enabled:
                for o in s['orders']:
                    if o['status']=='QUEUED' and o['side']=='BUY': o.update(status='CANCELLED',reasons=['OPERATOR_HALT'])
            self.store.put('state',s); self.store.append('risk.halt_changed',{'enabled':enabled})
        return self.snapshot()
    def cancel(self,order_id):
        with self.store.tx():
            s=self.s(); order=next((o for o in s['orders'] if o['id']==order_id),None)
            if not order or order['status']!='QUEUED': raise ValueError('Order is not queued')
            order['status']='CANCELLED'; self.store.put('state',s); self.store.append('order.cancelled',order)
        return order
    def configure(self,changes):
        allowed={'strategy','auto_orders','running','interval_seconds'}
        if set(changes)-allowed: raise ValueError('Unknown configuration field')
        if 'strategy' in changes and changes['strategy'] not in STRATEGIES: raise ValueError('Unknown strategy')
        if 'interval_seconds' in changes and not .5<=changes['interval_seconds']<=30: raise ValueError('Interval must be 0.5–30 seconds')
        with self.store.tx():
            s=self.s()
            if changes.get('running') or changes.get('auto_orders'): self._ensure_integrity()
            if changes.get('running') and s['halted']: raise ValueError('Resume the risk gate before starting replay')
            s.update(changes); self.store.put('state',s); self.store.append('configuration.updated',changes)
        return self.snapshot()
    def policy(self,changes):
        if set(changes)-set(DEFAULT_POLICY) or 'reset_timezone' in changes: raise ValueError('Unsupported policy field')
        for k,v in changes.items():
            if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v): raise ValueError('Finite numeric policy value required')
            if k.endswith('_pct') or k=='max_participation':
                if not 0<v<=1: raise ValueError('Fractional policy value outside (0,1]')
            elif k.endswith('_bps'):
                if not 0<=v<=100: raise ValueError('Cost must be 0–100 bps')
            elif not 1<=v<=1_000_000: raise ValueError('Invalid policy value')
        with self.store.tx():
            s=self.s()
            if s['running'] or any(o['status']=='QUEUED' for o in s['orders']): raise ValueError('Pause replay and cancel queued orders before changing policy')
            s['policy'].update(changes); self.store.put('state',s); self.store.append('policy.updated',s['policy'])
        return self.snapshot()
    def reset(self,dataset=None,capital=100000):
        if not math.isfinite(capital) or not 1000<=capital<=10_000_000: raise ValueError('Invalid starting cash')
        with self.store.tx():
            d=dataset or self.dataset; self.store.save_dataset(d); self.store.put('state',self.fresh(d,capital)); self.store.append('session.reset',{'dataset':d.manifest(),'initial_cash':capital}); self.dataset=d
        return self.snapshot()
    def alert(self,symbol,price,direction):
        if symbol not in self.dataset.symbols or direction not in ['above','below'] or not math.isfinite(price) or price<=0: raise ValueError('Invalid alert')
        a={'id':uid(),'symbol':symbol,'price':price,'direction':direction,'triggered':False}
        with self.store.tx():
            s=self.s()
            if len(s['alerts'])>=100: raise ValueError('Alert limit reached')
            s['alerts'].append(a); self.store.put('state',s); self.store.append('alert.created',a)
        return a
