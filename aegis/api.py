from __future__ import annotations
import asyncio, hmac, json, os, secrets, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from .engine import Service
from .data import Dataset, from_csv
from .quant import bootstrap, correlation, valuation
from .research import backtest
from .quant_eval import evaluate_candidates
from . import kronos_adapter as kronos
from .connectors import connector_status, sec_filings, alpaca_read
from .settings import build_runtime_context
from .doctor import run_doctor
from .market.read_model import LiveForecastSeries, MIN_LIVE_FORECAST_BARS, MarketReadModel
from .market.supervisor import MarketDataSupervisor
from .providers.alpaca_market import AlpacaMarketAdapter
from .providers.evidence import EvidenceStore
from .providers.sec import SecEdgarAdapter
from .providers.registry import provider_registry
from .llm.gateway import OpenRouterGateway
from .jobs.queue import DurableJobManager
from .agents.coordinator import ResearchCoordinator
from .risk.policy import DEFAULT_PROFILES, IndependentRiskGate, OrderIntent
from .brokers.alpaca_paper import AlpacaPaperOMS
from .brokers.context import OrderAuthorizationContext

ROOT=Path(__file__).resolve().parents[1]
class Model(BaseModel): model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
class Login(Model): token:str=Field(max_length=200)
class Step(Model): count:int=Field(default=1,ge=1,le=100)
class Order(Model):
    symbol:str=Field(max_length=12); side:Literal['BUY','SELL']; qty:int=Field(strict=True,ge=1,le=100000); client_id:str=Field(min_length=1,max_length=100); stop:float|None=None; target:float|None=None
    order_type:Literal['market','limit']='market'; limit_price:float|None=Field(default=None,gt=0)
class Config(Model):
    strategy:Literal['trend','reversion','ensemble']|None=None; auto_orders:bool|None=None; running:bool|None=None; interval_seconds:float|None=Field(default=None,ge=.5,le=30)
class Halt(Model): enabled:bool=True
class Backtest(Model): strategy:Literal['trend','reversion','ensemble']='ensemble'; cost_bps:float=Field(default=6,ge=0,le=100); walk_forward:bool=True
class Simulation(Model):
    symbol:str; horizon:int=Field(default=30,ge=2,le=120); trials:int=Field(default=1000,ge=100,le=10000); seed:int=Field(default=26,ge=0,le=2147483647); stop_pct:float=Field(default=.02,gt=0,lt=.5); target_pct:float=Field(default=.04,gt=0,lt=1)
class Forecast(Model): symbol:str; horizon:int=Field(default=12,ge=1,le=60); samples:int=Field(default=4,ge=2,le=16); seed:int=Field(default=26,ge=0,le=2147483647)
class Upload(Model): label:str=Field(default='Imported dataset',max_length=120); csv_text:str=Field(max_length=8000000); confirm_reset:bool=False
class Reset(Model): confirm:str; capital:float=Field(default=100000,ge=1000,le=10000000)
class Alert(Model): symbol:str; price:float=Field(gt=0); direction:Literal['above','below']
class DCF(Model): fcf:float=Field(gt=0); growth:float; discount:float; terminal_growth:float; years:int=Field(ge=1,le=15); shares:float=Field(gt=0); debt:float=Field(default=0,ge=0); cash:float=Field(default=0,ge=0)
class AgentJob(Model):
    symbol:str=Field(max_length=12); arm:Literal['deterministic_baseline','retrieval_assisted_llm','verified_multi_role']='deterministic_baseline'
    numerical_inputs:dict[str,float]|None=None

def create_app(db_path=None,token=None):
    wc=os.environ.get('WEB_CONCURRENCY','1')
    if wc.isdigit() and int(wc)>1 and os.environ.get('AEGIS_ALLOW_MULTIWORKER','').lower()!='true':
        raise RuntimeError('MULTIWORKER_BLOCKED: single-worker operational mode enforced until multiworker qualification is proven')
    settings,resolver=build_runtime_context()
    service=Service(db_path or str(settings.db_path))
    auth_token=token or resolver.resolve('AEGIS_TOKEN') or secrets.token_urlsafe(32)
    sessions={}; attempts={}; heavy=__import__('threading').Lock()
    with service.store.tx():
        s=service.s(); s['running']=False; service.store.put('state',s)
    @asynccontextmanager
    async def lifespan(app):
        async def clock():
            while True:
                await asyncio.sleep(.25)
                try:
                    s=service.s()
                    if s['running']:
                        await asyncio.sleep(s['interval_seconds']); s=service.s()
                        if s['running']: await asyncio.to_thread(service.advance,1)
                except asyncio.CancelledError: raise
                except Exception as exc:
                    with service.store.tx():
                        s=service.s(); s['running']=False; service.store.put('state',s); service.store.append('runtime.scheduler_error',{'error_type':type(exc).__name__})
        task=asyncio.create_task(clock())
        stream_stop=asyncio.Event()
        stream_task=None
        if settings.data_mode in ('live','delayed') and alpaca_market.credentials() is not None:
            def _persist_market_event(ev):
                market_read_model.ingest_event(ev)
                with service.store.tx():
                    service.store.append('market.event',ev.model_dump(mode='json'))
            stream_task=asyncio.create_task(alpaca_market.run_stream(list(service.dataset.symbols),on_event=_persist_market_event,stop_event=stream_stop))
        yield
        stream_stop.set()
        if stream_task is not None:
            stream_task.cancel()
            try: await stream_task
            except asyncio.CancelledError: pass
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    app=FastAPI(title='AEGIS QuantOS',version='0.2.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    supervisor=MarketDataSupervisor(feed=settings.alpaca_data_feed,source_mode=settings.data_mode,allow_synthetic_fallback=settings.allow_synthetic_fallback)
    if not (resolver.resolve('ALPACA_API_KEY') and resolver.resolve('ALPACA_SECRET_KEY')) and settings.data_mode in ('live','delayed'):
        supervisor.mark_blocked_credential('Alpaca market-data credentials missing')
    alpaca_market=AlpacaMarketAdapter(settings,resolver,supervisor)
    market_read_model=MarketReadModel(service.store)
    evidence_store=EvidenceStore(store=service.store)
    sec_adapter=SecEdgarAdapter(settings)
    llm_gateway=OpenRouterGateway(settings,resolver,store=service.store)
    job_manager=DurableJobManager(store=service.store)
    coordinator=ResearchCoordinator(evidence_store,llm_gateway)
    risk_gate=IndependentRiskGate(store=service.store)
    paper_oms=AlpacaPaperOMS(settings,resolver,store=service.store,risk_gate=risk_gate)
    app.state.service=service; app.state.token=auth_token; app.state.settings=settings; app.state.secrets=resolver; app.state.market_supervisor=supervisor; app.state.alpaca_market=alpaca_market; app.state.market_read_model=market_read_model; app.state.evidence_store=evidence_store; app.state.llm_gateway=llm_gateway; app.state.job_manager=job_manager; app.state.paper_oms=paper_oms; app.state.risk_gate=risk_gate
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver','[::1]'])
    @app.middleware('http')
    async def boundary(request:Request,call_next):
        origin=request.headers.get('origin')
        if origin and urlsplit(origin).netloc!=request.headers.get('host'):
            return JSONResponse({'detail':'Cross-origin access denied'},status_code=403)
        if request.url.path.startswith('/api/') and request.url.path not in ['/api/login','/api/health','/api/ready']:
            session=sessions.get(request.cookies.get('aegis_session'))
            if not session or session['expires']<time.time(): return JSONResponse({'detail':'Session required'},status_code=401)
            if request.method not in ['GET','HEAD'] and not hmac.compare_digest(request.headers.get('X-Aegis-CSRF',''),session['csrf']): return JSONResponse({'detail':'CSRF token required'},status_code=403)
        length=request.headers.get('content-length')
        if length and (not length.isdigit() or int(length)>9_000_000): return JSONResponse({'detail':'Request too large'},status_code=413)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'; response.headers['X-Frame-Options']='DENY'; response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
        return response
    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request,exc):
        return JSONResponse({'detail':[{'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in exc.errors()]},status_code=422)
    @app.exception_handler(ValueError)
    async def value_error(request,exc): return JSONResponse({'detail':str(exc)},status_code=400)
    @app.exception_handler(kronos.ModelUnavailable)
    async def model_error(request,exc): return JSONResponse({'detail':str(exc),'state':'UNAVAILABLE'},status_code=503)
    @app.exception_handler(httpx.HTTPError)
    async def external_error(request,exc): return JSONResponse({'detail':'External provider request failed. Check network, permissions and provider status.','error_type':type(exc).__name__},status_code=502)
    @app.get('/api/health')
    def health():return {'status':'ok','version':'0.2.0','mode':'PAPER_ONLY','live_execution':False}
    @app.get('/api/ready')
    def ready():
        doc=run_doctor(settings,resolver,check_network=False)
        return {'process':'ok','data_mode':settings.data_mode,'execution_mode':settings.execution_mode,'live_execution':False,'providers':{k:{'configured':v['configured'],'qualified':v['qualified']} for k,v in doc['providers'].items()}}
    @app.post('/api/login')
    def login(body:Login,request:Request):
        host=request.client.host if request.client else 'unknown'; now=time.time(); recent=[t for t in attempts.get(host,[]) if now-t<60]; attempts[host]=recent+[now]
        if len(recent)>=10: raise HTTPException(429,'Too many login attempts')
        if not hmac.compare_digest(body.token,auth_token): raise HTTPException(401,'Invalid access token')
        sid=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); sessions[sid]={'csrf':csrf,'expires':now+8*3600}
        response=JSONResponse({'csrf':csrf,'expires_in':28800}); response.set_cookie('aegis_session',sid,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=28800); return response
    @app.get('/api/session')
    def session(request:Request):return {'csrf':sessions[request.cookies['aegis_session']]['csrf']}
    @app.post('/api/logout')
    def logout(request:Request):
        sessions.pop(request.cookies.get('aegis_session'),None); r=JSONResponse({'ok':True}); r.delete_cookie('aegis_session'); return r
    @app.get('/api/state')
    def state():
        snap=service.snapshot()
        ctrl=risk_gate.get_control_state(environment=settings.execution_mode)
        snap['execution_mode']=settings.execution_mode
        snap['control_state']=ctrl
        if settings.execution_mode=='paper_external' and ctrl['halted']:
            snap['halted']=True
            snap['halt_reason']=ctrl['halt_reason']
        if settings.data_mode!='synthetic':
            sup=supervisor.status()
            msnap=market_read_model.snapshot(source_mode=settings.data_mode,feed=settings.alpaca_data_feed,feed_state=str(sup['state']),max_age_seconds=risk_gate.profile.max_data_age_seconds)
            snap['mode']=f'{settings.data_mode.upper()}_MARKET'
            snap['data_status']=sup['state']
            snap['dataset']['source']=f'market_read_model:{settings.data_mode}'
            snap['scanner']=market_read_model.scanner(source_mode=settings.data_mode,feed=settings.alpaca_data_feed)
            snap['market_snapshot']=msnap.model_dump(mode='json')
        return snap
    @app.get('/api/market/feed/status')
    def market_feed_status():return supervisor.status()
    @app.get('/api/market/{symbol}')
    def market(symbol:str):
        sym=symbol.upper()
        if settings.data_mode!='synthetic':
            bars=market_read_model.bars(sym,source_mode=settings.data_mode,feed=settings.alpaca_data_feed,limit=240)
            return {'symbol':sym,'source':f'market_read_model:{settings.data_mode}','source_mode':settings.data_mode,'status':supervisor.status()['state'],'bars':bars,'asof':bars[-1]['timestamp'] if bars else None}
        with service.store.lock:
            s=service.s()
            if symbol not in service.dataset.symbols: raise ValueError('Unknown symbol')
            return {'symbol':symbol,'source':service.dataset.source,'bars':service.dataset.prefix(symbol,s['index'])[-240:],'asof':service.dataset.time(s['index'])}
    @app.post('/api/orders')
    def order(body:Order):
        if settings.execution_mode=='paper_external':
            if body.stop is not None or body.target is not None:
                raise ValueError('UNSUPPORTED_PROTECTIVE_PARAMETERS: paper_external does not support protective stop/target bracket orders')
            if settings.data_mode=='synthetic':
                raise ValueError('MARKET_INTEGRITY_FAILED: synthetic market data cannot authorize paper_external orders')
            now=datetime.now(timezone.utc)
            sup=supervisor.status(now)
            msnap=market_read_model.snapshot(source_mode=settings.data_mode,feed=settings.alpaca_data_feed,feed_state=str(sup['state']),max_age_seconds=risk_gate.profile.max_data_age_seconds,now=now)
            sym=body.symbol.upper()
            mark=msnap.marks.get(sym)
            if mark is None or mark.price<=0:
                if not msnap.integrity_ok:
                    raise ValueError(f"MARKET_INTEGRITY_FAILED: feed={msnap.feed_state} marks={len(msnap.marks)}")
                raise ValueError(f'STALE_OR_MISSING_MARK: {sym}')
            acct=paper_oms.fetch_account_snapshot()
            if not acct.integrity_ok:
                raise ValueError(f'EXTERNAL_ACCOUNT_UNHEALTHY: status={acct.status}')
            if not msnap.integrity_ok and body.side!='SELL':
                raise ValueError(f"MARKET_INTEGRITY_FAILED: feed={msnap.feed_state} marks={len(msnap.marks)}")
            if mark.stale or mark.age_seconds is None:
                if body.side!='SELL':
                    raise ValueError(f'STALE_OR_MISSING_MARK: {sym}')
            auth_ctx=OrderAuthorizationContext(account=acct,market=msnap,policy_version=risk_gate.profile.profile_id)
            data_age=float(mark.age_seconds) if mark.age_seconds is not None else float(risk_gate.profile.max_data_age_seconds+1.0)
            intent=OrderIntent(account_id=acct.account_id,environment='paper_external',symbol=sym,side=body.side,qty=float(body.qty),order_type=body.order_type,limit_price=body.limit_price,stop_price=body.stop,take_profit_price=body.target,client_order_id=body.client_id,expires_at=now+timedelta(minutes=5),data_age_seconds=data_age,policy_version=risk_gate.profile.profile_id)
            marks_dict={s:m.price for s,m in msnap.marks.items()}
            decision=risk_gate.evaluate(intent,price=mark.price,equity=acct.equity,cash=acct.cash,positions=acct.positions,marks=marks_dict,daily_pnl_pct=acct.daily_pnl_pct,total_drawdown_pct=acct.total_drawdown_pct,halted=None,integrity_ok=acct.integrity_ok and msnap.integrity_ok,account_revision=acct.account_revision,now=now)
            if not decision.approved or decision.intent_hash!=intent.intent_hash():
                raise ValueError('RISK_REJECTED: '+','.join(decision.reasons))
            return {'mode':'paper_external','authorization_context':auth_ctx.model_dump(mode='json'),'decision':decision.model_dump(mode='json'),'order':paper_oms.submit_order(intent,decision).model_dump(mode='json')}
        if body.order_type!='market' or body.limit_price is not None:
            raise ValueError('UNSUPPORTED_ORDER_TYPE: paper_local simulation only supports market orders without limit_price')
        return service.submit(**body.model_dump(exclude={'order_type','limit_price'}))
    @app.post('/api/orders/{ident}/cancel')
    def cancel(ident:str):
        if settings.execution_mode=='paper_external':
            return paper_oms.cancel_order(ident).model_dump(mode='json')
        return service.cancel(ident)
    @app.post('/api/replay/step')
    def step(body:Step):return service.advance(body.count)
    @app.post('/api/research/cycle')
    def cycle():return service.cycle()
    @app.post('/api/config')
    def config(body:Config):return service.configure(body.model_dump(exclude_none=True))
    @app.post('/api/risk/halt')
    def halt(body:Halt):
        ctrl=risk_gate.set_halt(body.enabled,reason='OPERATOR_HALT' if body.enabled else None)
        snap=service.halt(body.enabled)
        snap['control_state']=ctrl
        snap['execution_mode']=settings.execution_mode
        return snap
    @app.post('/api/risk/policy')
    def policy(body:dict):
        if settings.execution_mode=='paper_external':
            ctrl=risk_gate.update_profile(body)
            return {'execution_mode':'paper_external','profile':risk_gate.profile.model_dump(mode='json'),'control_state':ctrl}
        return service.policy(body)
    @app.post('/api/reset')
    def reset(body:Reset):
        if body.confirm!='RESET PAPER SESSION': raise ValueError('Explicit reset confirmation required')
        return service.reset(capital=body.capital)
    @app.post('/api/data/import')
    def upload(body:Upload):
        if not body.confirm_reset: raise ValueError('Import creates a new paper session; explicit confirmation required')
        return service.reset(from_csv(body.csv_text,body.label))
    @app.get('/api/data/export')
    def export_data():return Response(service.dataset.csv(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="aegis_dataset.csv"'})
    @app.post('/api/backtests')
    def run_backtest(body:Backtest):
        if not heavy.acquire(False): raise HTTPException(409,'A research job is already running')
        try:
            if service.dataset.length>1000: raise ValueError('Interactive backtests limited to 1,000 aligned bars per symbol')
            result=backtest(service.dataset,**body.model_dump())
            with service.store.tx(): result=service.store.result('backtest',result); service.store.append('research.backtest_completed',{'id':result['id'],'hash':result['run_hash']})
            return result
        finally:heavy.release()
    @app.post('/api/simulations')
    def simulation(body:Simulation):
        s=service.s()
        if body.symbol not in service.dataset.symbols: raise ValueError('Unknown symbol')
        result=bootstrap(service.dataset.prefix(body.symbol,s['index']),**body.model_dump(exclude={'symbol'})); result.update(symbol=body.symbol,asof=service.dataset.time(s['index']),dataset_id=service.dataset.id)
        with service.store.tx(): result=service.store.result('simulation',result); service.store.append('research.simulation_completed',{'id':result['id'],'trials':result['trials']})
        return result
    @app.post('/api/forecasts')
    def forecast(body:Forecast):
        if not heavy.acquire(False):raise HTTPException(409,'A research job is already running')
        try:
            if settings.data_mode!='synthetic':
                sym=body.symbol.upper()
                now_utc=datetime.now(timezone.utc)
                bars=market_read_model.bars(sym,source_mode=settings.data_mode,feed=settings.alpaca_data_feed,as_of=now_utc,limit=240)
                tf=bars[-1]['timeframe'] if bars else '1Min'
                ds=LiveForecastSeries(symbol=sym,bars=bars,source_mode=settings.data_mode,timeframe=tf,as_of=now_utc,min_bars=MIN_LIVE_FORECAST_BARS)
                result=kronos.forecast(ds,ds.length-1,**body.model_dump(),use_exchange_calendar=True,bar_minutes=ds.bar_minutes)
                result['input_bar_hashes']=[b['raw_payload_hash'] for b in bars[-10:]]
                result['timeframe']=tf
                result['provider']=bars[-1]['provider']
                result['feed']=bars[-1]['feed']
                result['source_mode']=settings.data_mode
            else:
                s=service.s(); result=kronos.forecast(service.dataset,s['index'],**body.model_dump())
            with service.store.tx(): result=service.store.result('forecast',result); service.store.append('model.forecast_completed',{'id':result['id'],'model':result['model'],'execution_eligible':result['execution_eligible']})
            return result
        finally:heavy.release()
    @app.get('/api/research/results')
    def results():return service.store.results()
    @app.post('/api/research/evaluate')
    def evaluate_research():
        return evaluate_candidates(service.dataset)
    @app.get('/api/correlation')
    def correlations():return correlation(service.dataset,service.s()['index'])
    @app.post('/api/valuation')
    def dcf(body:DCF):
        result=valuation(**body.model_dump())
        with service.store.tx():result=service.store.result('valuation',result); service.store.append('research.valuation',{'id':result['id']})
        return result
    @app.post('/api/alerts')
    def alerts(body:Alert):return service.alert(**body.model_dump())
    @app.get('/api/doctor')
    def doctor():return run_doctor(settings,resolver,check_network=False)
    @app.get('/api/integrations')
    def integrations():
        doc=run_doctor(settings,resolver,check_network=False)
        return {'kronos':kronos.status(),'connectors':connector_status(),'providers':doc['providers'],'settings':doc['settings'],'external_order_submission':False,'original_project_repositories':'NOT_AVAILABLE; native scoped modules, not imported private code'}
    @app.get('/api/filings/{cik}')
    def filings(cik:str):
        result=sec_filings(cik)
        with service.store.tx(): result=service.store.result('filings',result); service.store.append('research.filings_imported',{'id':result['id'],'hash':result['response_sha256']})
        return result
    @app.get('/api/sec/facts/{cik}')
    def sec_facts(cik:str):
        summary,doc=sec_adapter.fetch_company_facts(cik)
        evidence_store.add(doc)
        return {'summary':summary,'document':doc.model_dump(mode='json')}
    @app.get('/api/evidence/documents')
    def evidence_documents(symbol:str|None=None,as_of:str|None=None):
        if as_of:
            from datetime import datetime
            cutoff=datetime.fromisoformat(as_of.replace('Z','+00:00'))
            docs=evidence_store.as_of(cutoff,symbol=symbol)
        else:
            docs=[d for d in evidence_store.all_documents() if not symbol or d.entity_symbol==symbol.upper()]
        return {'count':len(docs),'documents':[d.model_dump(mode='json') for d in docs],'registry':provider_registry(settings,resolver)}
    @app.get('/api/agents/jobs')
    def list_agent_jobs():return {'jobs':job_manager.list_jobs()}
    @app.post('/api/agents/jobs')
    def run_agent_job(body:AgentJob):
        from datetime import datetime,timezone
        rec=job_manager.create('research_arm',body.symbol)
        job_manager.record_step(rec.job_id,'dispatch',{'arm':body.arm})
        docs_now=evidence_store.as_of(datetime.now(timezone.utc),symbol=body.symbol.upper())
        if body.numerical_inputs is None and not docs_now and body.arm=='deterministic_baseline':
            out={'arm':body.arm,'job_id':rec.job_id,'symbol':body.symbol.upper(),'status':'ABSTAINED','abstain_reason':'No sourced numerical valuation inputs or point-in-time evidence available','evidence_count':0,'llm_calls':0,'cost_usd':0.0}
            job_manager.finish(rec.job_id,'ABSTAINED',result=out)
            return job_manager.get(rec.job_id).model_dump(mode='json')
        if body.numerical_inputs is not None:
            required={'fcf','growth','discount','terminal_growth','years','shares'}
            missing=sorted(required-set(body.numerical_inputs.keys()))
            if missing:
                raise ValueError('Missing valuation inputs: '+','.join(missing))
        out=coordinator.run_arm(arm=body.arm,job_id=rec.job_id,symbol=body.symbol,numerical_inputs=body.numerical_inputs)
        st='COMPLETED' if out.get('status')=='COMPLETED' else ('ABSTAINED' if out.get('status')=='ABSTAINED' else 'BLOCKED')
        job_manager.finish(rec.job_id,st,result=out)
        return job_manager.get(rec.job_id).model_dump(mode='json')
    @app.post('/api/agents/jobs/{job_id}/cancel')
    def cancel_agent_job(job_id:str):
        if not job_manager.get(job_id): raise HTTPException(404,'Job not found')
        return job_manager.cancel(job_id).model_dump(mode='json')
    @app.get('/api/llm/status')
    def llm_status():
        return {
            'provider': settings.llm_provider,
            'paid_calls_authorized': settings.enable_paid_llm,
            'key_state': resolver.status('OPENROUTER_API_KEY').state,
            'models_lock': llm_gateway.lock,
            'budget': llm_gateway.budget.snapshot(),
        }
    @app.get('/api/risk/profiles')
    def risk_profiles():return {k:v.model_dump(mode='json') for k,v in DEFAULT_PROFILES.items()}
    @app.get('/api/broker/paper-orders')
    def paper_orders():return {'execution_mode':settings.execution_mode,'live_money_enabled':False,'outbox':paper_oms.outbox_snapshot()}
    @app.post('/api/broker/paper-orders/{client_order_id}/reconcile')
    def reconcile_paper_order(client_order_id:str):return paper_oms.reconcile_by_client_id(client_order_id).model_dump(mode='json')
    @app.post('/api/broker/paper-orders/{client_order_id}/cancel')
    def cancel_paper_order(client_order_id:str):return paper_oms.cancel_order(client_order_id).model_dump(mode='json')
    @app.get('/api/broker/paper-account')
    def broker():return alpaca_read()
    @app.get('/api/audit')
    def audit():return {'verification':service.store.verify(),'reconciliation':service.reconcile(),'events':service.store.events(250)}
    @app.get('/api/reconciliation')
    def reconciliation():return service.reconcile()
    @app.get('/api/audit/export')
    def audit_export():
        with service.store.lock:
            result={'anchor':service.store.verify(),'events':list(reversed(service.store.events(1000000))),'dataset':service.dataset.manifest()}
        return Response(json.dumps(result,indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="aegis_evidence.json"'})
    @app.get('/api/stream')
    async def stream(request:Request):
        async def events():
            last=None
            while not await request.is_disconnected():
                head=service.store.get('ledger_head')
                if head!=last:
                    yield 'event: update\ndata: '+json.dumps(head)+'\n\n'; last=head
                else:yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(events(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
    @app.get('/')
    def index():return FileResponse(ROOT/'web'/'index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')
    return app
