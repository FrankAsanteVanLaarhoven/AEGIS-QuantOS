from __future__ import annotations
import asyncio, hmac, json, os, secrets, time
from contextlib import asynccontextmanager
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
from .data import from_csv
from .quant import bootstrap, correlation, valuation
from .research import backtest
from .quant_eval import evaluate_candidates
from . import kronos_adapter as kronos
from .connectors import connector_status, sec_filings, alpaca_read
from .settings import build_runtime_context
from .doctor import run_doctor
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

ROOT=Path(__file__).resolve().parents[1]
class Model(BaseModel): model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
class Login(Model): token:str=Field(max_length=200)
class Step(Model): count:int=Field(default=1,ge=1,le=100)
class Order(Model):
    symbol:str=Field(max_length=12); side:Literal['BUY','SELL']; qty:int=Field(strict=True,ge=1,le=100000); client_id:str=Field(min_length=1,max_length=100); stop:float|None=None; target:float|None=None
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
class AgentJob(Model): symbol:str=Field(max_length=12); arm:Literal['deterministic_baseline','retrieval_assisted_llm','verified_multi_role']='deterministic_baseline'

def create_app(db_path=None,token=None):
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
        yield
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    app=FastAPI(title='AEGIS QuantOS',version='0.2.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    supervisor=MarketDataSupervisor(feed=settings.alpaca_data_feed,source_mode=settings.data_mode,allow_synthetic_fallback=settings.allow_synthetic_fallback)
    if not (resolver.resolve('ALPACA_API_KEY') and resolver.resolve('ALPACA_SECRET_KEY')) and settings.data_mode in ('live','delayed'):
        supervisor.mark_blocked_credential('Alpaca market-data credentials missing')
    alpaca_market=AlpacaMarketAdapter(settings,resolver,supervisor)
    evidence_store=EvidenceStore()
    sec_adapter=SecEdgarAdapter(settings)
    llm_gateway=OpenRouterGateway(settings,resolver,store=service.store)
    job_manager=DurableJobManager()
    coordinator=ResearchCoordinator(evidence_store,llm_gateway)
    risk_gate=IndependentRiskGate(store=service.store)
    paper_oms=AlpacaPaperOMS(settings,resolver,store=service.store,risk_gate=risk_gate)
    app.state.service=service; app.state.token=auth_token; app.state.settings=settings; app.state.secrets=resolver; app.state.market_supervisor=supervisor; app.state.evidence_store=evidence_store; app.state.llm_gateway=llm_gateway; app.state.job_manager=job_manager; app.state.paper_oms=paper_oms; app.state.risk_gate=risk_gate
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
    def state():return service.snapshot()
    @app.get('/api/market/feed/status')
    def market_feed_status():return supervisor.status()
    @app.get('/api/market/{symbol}')
    def market(symbol:str):
        with service.store.lock:
            s=service.s()
            if symbol not in service.dataset.symbols: raise ValueError('Unknown symbol')
            return {'symbol':symbol,'source':service.dataset.source,'bars':service.dataset.prefix(symbol,s['index'])[-240:],'asof':service.dataset.time(s['index'])}
    @app.post('/api/orders')
    def order(body:Order):
        if settings.execution_mode=='paper_external':
            from datetime import datetime,timedelta,timezone
            snap=service.snapshot()
            marks={sym:float(q['close']) for sym,q in snap.get('quotes',{}).items() if 'close' in q}
            price=marks.get(body.symbol.upper())
            if not price: raise ValueError('Unknown symbol or missing mark')
            positions={sym:float(pos.get('qty',0.0)) for sym,pos in snap.get('positions',{}).items()}
            acct=snap.get('account',{})
            now=datetime.now(timezone.utc)
            intent=OrderIntent(environment='paper_external',symbol=body.symbol.upper(),side=body.side,qty=float(body.qty),client_order_id=body.client_id,expires_at=now+timedelta(minutes=5),data_age_seconds=float(supervisor.snapshot().get('data_age_seconds') or 0.0))
            decision=risk_gate.evaluate(intent,price=price,equity=float(acct.get('equity',0.0)),cash=float(acct.get('cash',0.0)),positions=positions,marks=marks,halted=bool(snap.get('halted',False)),integrity_ok=bool(snap.get('integrity_ok',True)),now=now)
            if not decision.approved: raise ValueError('RISK_REJECTED: '+','.join(decision.reasons))
            return {'mode':'paper_external','decision':decision.model_dump(mode='json'),'order':paper_oms.submit_order(intent,decision).model_dump(mode='json')}
        return service.submit(**body.model_dump())
    @app.post('/api/orders/{ident}/cancel')
    def cancel(ident:str):return service.cancel(ident)
    @app.post('/api/replay/step')
    def step(body:Step):return service.advance(body.count)
    @app.post('/api/research/cycle')
    def cycle():return service.cycle()
    @app.post('/api/config')
    def config(body:Config):return service.configure(body.model_dump(exclude_none=True))
    @app.post('/api/risk/halt')
    def halt(body:Halt):return service.halt(body.enabled)
    @app.post('/api/risk/policy')
    def policy(body:dict):return service.policy(body)
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
        rec=job_manager.create('research_arm',body.symbol)
        job_manager.record_step(rec.job_id,'dispatch',{'arm':body.arm})
        out=coordinator.run_arm(arm=body.arm,job_id=rec.job_id,symbol=body.symbol,numerical_inputs={'fcf':120,'growth':0.08,'discount':0.11,'terminal_growth':0.03,'years':5,'shares':10})
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
