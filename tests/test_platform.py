from __future__ import annotations
import json, math, copy, os, tempfile
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from aegis.data import synthetic, Dataset, from_csv, digest
from aegis.engine import Service, D
from aegis.quant import signal, bootstrap, valuation, correlation
from aegis.api import create_app
from aegis.kronos_adapter import forecast, ModelUnavailable, verify_artifacts, COMMIT
from aegis.research import backtest

@pytest.fixture
def svc():
    s=Service(':memory:');yield s;s.store.db.close()
@pytest.fixture
def data():return synthetic()
@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'test.sqlite3',token='test-access-token')
    with TestClient(app) as c:
        r=c.post('/api/login',json={'token':'test-access-token'}); assert r.status_code==200
        c.headers['X-Aegis-CSRF']=r.json()['csrf'];yield c,app.state.service

class TestData:
    def test_seed_determinism(self):assert synthetic(26).id==synthetic(26).id
    def test_seed_changes(self):assert synthetic(26).id!=synthetic(27).id
    def test_manifest_full_digest(self,data):assert len(data.manifest()['sha256'])==64
    def test_csv_roundtrip(self,data):
        d=from_csv(data.csv(),'roundtrip');assert d.length==data.length;assert d.groups==data.groups
    @pytest.mark.parametrize('field,value',[('open',float('nan')),('high',float('inf')),('low',0),('close',-1),('volume',-1),('volume',1.5)])
    def test_bad_numeric(self,data,field,value):
        rows=data.to_rows();rows[0]=dict(rows[0],**{field:value})
        with pytest.raises(ValueError):Dataset(rows)
    def test_bad_ohlc(self,data):
        rows=data.to_rows();rows[0]=dict(rows[0],high=.1)
        with pytest.raises(ValueError):Dataset(rows)
    def test_duplicate_bar(self,data):
        with pytest.raises(ValueError):Dataset(data.to_rows()+[data.to_rows()[0]])
    def test_naive_timestamp(self,data):
        rows=data.to_rows();rows[0]=dict(rows[0],timestamp='2026-11-02T14:34:59')
        with pytest.raises(ValueError):Dataset(rows)
    def test_early_available(self,data):
        rows=data.to_rows();rows[0]=dict(rows[0],available_at='2026-11-02T14:00:00Z')
        with pytest.raises(ValueError):Dataset(rows)
    def test_misaligned_universe(self,data):
        with pytest.raises(ValueError):Dataset(data.to_rows()[1:])
    def test_corporate_action_rejected(self,data):
        rows=data.to_rows();rows[0]=dict(rows[0],split_factor=2)
        with pytest.raises(ValueError):Dataset(rows)
    def test_missing_columns(self):
        with pytest.raises(ValueError):from_csv('symbol,close\nNVDA,100','bad')
    def test_future_mutation_no_signal_change(self,data):
        before=signal(data.prefix('NVDA',59));data.groups['NVDA'][120]['close']*=100
        assert signal(data.prefix('NVDA',59))==before
    def test_no_same_bar_future_access(self,data):assert all(r['available_at']<=data.time(59) for r in data.prefix('NVDA',59))

class TestOrders:
    def test_next_open_not_same_close(self,svc):
        o=svc.submit('NVDA','BUY',2,'key');assert o['status']=='QUEUED';assert svc.s()['cash']=='100000.000000'
        snap=svc.advance();o=snap['orders'][0];assert o['status']=='FILLED';assert o['filled_at']==svc.dataset.row('NVDA',60)['open_at'];assert o['fill_price']>svc.dataset.row('NVDA',60)['open']
    def test_cash_conservation(self,svc):
        svc.submit('NVDA','BUY',2,'one');s=svc.advance();o=s['orders'][0]
        assert abs(s['stats']['cash']-(100000-o['fill_price']*2-o['fee']))<1e-5
    def test_duplicate_idempotent(self,svc):
        a=svc.submit('NVDA','BUY',2,'same');b=svc.submit('NVDA','BUY',2,'same');assert a==b;assert len(svc.s()['orders'])==1
    def test_idempotency_conflict(self,svc):
        svc.submit('NVDA','BUY',2,'same')
        with pytest.raises(ValueError):svc.submit('NVDA','BUY',3,'same')
    def test_concurrent_idempotency(self,svc):
        with ThreadPoolExecutor(8) as ex:results=list(ex.map(lambda _:svc.submit('NVDA','BUY',2,'same'),range(20)))
        assert len({x['id'] for x in results})==1;assert len(svc.s()['orders'])==1
    def test_insufficient_cash(self,svc):
        svc.reset(capital=1000);o=svc.submit('NVDA','BUY',20,'big');assert o['status']=='REJECTED';assert 'INSUFFICIENT_CASH' in o['reasons']
    def test_cross_symbol_gross(self,svc):
        svc.policy({'max_gross_pct':.08});assert svc.submit('NVDA','BUY',40,'a')['status']=='QUEUED'
        o=svc.submit('AMAT','BUY',40,'b');assert 'GROSS_EXPOSURE_LIMIT' in o['reasons']
    def test_queued_concentration(self,svc):
        svc.policy({'max_order_notional':30000})
        assert svc.submit('NVDA','BUY',100,'a')['status']=='QUEUED'
        assert 'POSITION_LIMIT' in svc.submit('NVDA','BUY',100,'b')['reasons']
    def test_no_short(self,svc):assert 'SHORT_SELLING_DISABLED' in svc.submit('NVDA','SELL',1,'s')['reasons']
    def test_halt_cancels_pending_buys(self,svc):
        svc.submit('NVDA','BUY',2,'b');svc.halt();assert svc.s()['orders'][0]['status']=='CANCELLED';assert svc.submit('NVDA','BUY',2,'c')['status']=='REJECTED'
    def test_reduce_when_halted(self,svc):
        svc.submit('NVDA','BUY',2,'b');svc.advance();svc.halt();assert svc.submit('NVDA','SELL',2,'s')['status']=='QUEUED';assert not svc.advance()['positions']
    def test_overselling_pending_rejected(self,svc):
        svc.submit('NVDA','BUY',2,'b');svc.advance();svc.submit('NVDA','SELL',2,'s');assert svc.submit('NVDA','SELL',2,'s2')['status']=='REJECTED'
    def test_cancel_releases_reservation(self,svc):
        a=svc.submit('NVDA','BUY',2,'b');svc.cancel(a['id']);assert svc.s()['orders'][0]['status']=='CANCELLED';assert svc.s()['cash']=='100000.000000'
    def test_partial_fill_and_shared_participation(self,svc):
        svc.policy({'max_participation':.00001});o=svc.submit('NVDA','BUY',10,'a');svc.submit('NVDA','BUY',10,'b');s=svc.advance();limit=int(svc.dataset.row('NVDA',60)['volume']*.00001);assert sum(o['filled_qty'] for o in s['orders'])<=limit
    def test_policy_change_with_pending_rejected(self,svc):
        svc.submit('NVDA','BUY',1,'b')
        with pytest.raises(ValueError):svc.policy({'max_order_notional':5000})
    @pytest.mark.parametrize('qty',[0,-1,True,1.5])
    def test_invalid_quantity(self,svc,qty):
        with pytest.raises(ValueError):svc.submit('NVDA','BUY',qty,'bad')
    def test_bad_stop(self,svc):
        with pytest.raises(ValueError):svc.submit('NVDA','BUY',1,'bad',stop=10000)
    def test_stop_gap_revalidated(self,svc):
        price=svc.dataset.row('NVDA',59)['close'];svc.submit('NVDA','BUY',2,'b',stop=price*.99)
        svc.dataset.groups['NVDA'][60]['open']=price*.95
        assert svc.advance()['orders'][0]['status']=='REJECTED'
    def test_persist_restart(self,tmp_path):
        p=tmp_path/'state.db';s=Service(p);s.submit('NVDA','BUY',2,'b');s.advance();before=s.snapshot();s.store.db.close();t=Service(p);after=t.snapshot();assert before['stats']==after['stats'];assert after['audit']['valid'];t.store.db.close()
    def test_risk_rolls_back_on_exception(self,svc):
        before=svc.snapshot();
        with pytest.raises(ValueError):svc.submit('UNKNOWN','BUY',2,'bad')
        assert before==svc.snapshot()
    def test_daily_limit_halt(self,svc):
        svc.submit('NVDA','BUY',50,'b');svc.advance();svc.dataset.groups['NVDA'][61]['close']=10
        assert svc.advance()['halted']
    def test_reset_retains_evidence(self,svc):
        n=svc.store.verify()['events'];svc.reset();assert svc.store.verify()['events']==n+1
    def test_bar_end(self,svc):
        with svc.store.tx():s=svc.s();s['index']=svc.dataset.length-1;svc.store.put('state',s)
        assert 'END_OF_DATA' in svc.submit('NVDA','BUY',1,'e')['reasons'];assert svc.advance()['remaining_bars']==0

class TestResearch:
    def test_bootstrap_reproducible(self,data):
        a=bootstrap(data.prefix('NVDA',59),trials=100,horizon=10);b=bootstrap(data.prefix('NVDA',59),trials=100,horizon=10);assert a==b
    def test_scenario_sum(self,data):
        r=bootstrap(data.prefix('NVDA',59),trials=100,horizon=10);assert math.isclose(r['target_first']+r['stop_first']+r['unresolved'],1);assert not r['calibrated']
    def test_bands_ordered(self,data):
        r=bootstrap(data.prefix('NVDA',59),trials=100,horizon=10);assert all(b['low']<=b['median']<=b['high'] for b in r['bands'])
    def test_valuation_known(self):
        r=valuation(100,0,.1,0,5,10);assert abs(r['per_share']-100)<1e-8
    def test_valuation_invalid_terminal(self):
        with pytest.raises(ValueError):valuation(100,.05,.02,.04,5,10)
    def test_correlation_symmetric(self,data):
        r=correlation(data,59);m=r['matrix'];assert r['observations']==59
        assert all(abs(m[i][j]-m[j][i])<1e-12 for i in range(5) for j in range(5));assert all(abs(m[i][i]-1)<1e-12 for i in range(5))
    def test_backtest_repeatable(self,data):
        a=backtest(data,walk_forward=False);b=backtest(data,walk_forward=False);assert a['run_hash']==b['run_hash'];assert a['net_return']==b['net_return'];assert a['annualized_sharpe'] is None;assert a['ledger_valid']
    def test_model_missing_fail_closed(self,data,tmp_path,monkeypatch):
        monkeypatch.setenv('AEGIS_MODELS_DIR',str(tmp_path))
        with pytest.raises(ModelUnavailable):forecast(data,59,'NVDA')
    def test_artifact_manifest_wrong_commit(self):
        with pytest.raises(ModelUnavailable):verify_artifacts({'source_commit':'wrong'})
    def test_agent_cycle_records_trace(self,svc):assert len(svc.cycle()['trace'])==6
    def test_alert_trigger(self,svc):
        svc.alert('NVDA',1,'above');assert svc.advance()['alerts'][0]['triggered']

class TestEvidence:
    def test_hash_chain(self,svc):
        svc.submit('NVDA','BUY',2,'a');svc.advance();assert svc.store.verify()['valid']
    def test_payload_tamper(self,svc):
        svc.store.db.execute("UPDATE events SET payload='{}' WHERE seq=1");assert not svc.store.verify()['valid']
    def test_tail_truncation(self,svc):
        svc.advance();svc.store.db.execute('DELETE FROM events WHERE seq=(SELECT max(seq) FROM events)');assert not svc.store.verify()['valid']
    def test_transaction_atomicity(self,svc):
        n=svc.store.verify()['events']
        with pytest.raises(RuntimeError):
            with svc.store.tx():svc.store.append('test',{});raise RuntimeError('crash')
        assert svc.store.verify()['events']==n;assert svc.store.verify()['valid']

class TestAPI:
    def test_auth_required(self,tmp_path):
        c=TestClient(create_app(tmp_path/'db',token='s'));assert c.get('/api/state').status_code==401
    def test_health_and_assets(self,client):
        c,s=client;assert c.get('/api/health').json()['live_execution'] is False;assert c.get('/').status_code==200;assert c.get('/static/app.js').status_code==200
    def test_csrf(self,client):
        c,s=client;assert c.post('/api/replay/step',json={},headers={'X-Aegis-CSRF':''}).status_code==403
    def test_origin(self,client):
        c,s=client;assert c.get('/api/state',headers={'Origin':'https://evil.example'}).status_code==403
    def test_host(self,client):
        c,s=client;assert c.get('/api/state',headers={'Host':'evil.example'}).status_code==400
    def test_order_fill_end_to_end(self,client):
        c,s=client;o=c.post('/api/orders',json={'symbol':'NVDA','side':'BUY','qty':2,'client_id':'e2e'});assert o.json()['status']=='QUEUED';r=c.post('/api/replay/step',json={});assert r.json()['orders'][0]['status']=='FILLED'
    def test_decimal_quantity(self,client):
        c,s=client;assert c.post('/api/orders',json={'symbol':'NVDA','side':'BUY','qty':1.5,'client_id':'bad'}).status_code==422
    def test_unknown_property(self,client):
        c,s=client;assert c.post('/api/orders',json={'symbol':'NVDA','side':'BUY','qty':1,'client_id':'bad','live':True}).status_code==422
    def test_no_live_route(self,client):
        c,s=client;assert c.post('/api/live/orders',json={}).status_code==404
    def test_import_requires_confirmation(self,client):
        c,s=client;assert c.post('/api/data/import',json={'csv_text':s.dataset.csv()}).status_code==400
    def test_audit_export(self,client):
        c,s=client;r=c.get('/api/audit/export');assert r.status_code==200;assert r.json()['anchor']['valid']
    def test_kronos_unavailable(self,client,tmp_path,monkeypatch):
        c,s=client;monkeypatch.setenv('AEGIS_MODELS_DIR',str(tmp_path));assert c.post('/api/forecasts',json={'symbol':'NVDA'}).status_code==503
    def test_simulation_api(self,client):
        c,s=client;r=c.post('/api/simulations',json={'symbol':'NVDA','trials':100,'horizon':5});assert r.status_code==200;assert r.json()['trials']==100
    def test_dcf_api(self,client):
        c,s=client;r=c.post('/api/valuation',json={'fcf':100,'growth':0,'discount':.1,'terminal_growth':0,'years':5,'shares':10});assert r.status_code==200;assert abs(r.json()['per_share']-100)<1e-8
    def test_logout(self,client):
        c,s=client;assert c.post('/api/logout',json={}).status_code==200;assert c.get('/api/state').status_code==401
    def test_reset_confirm(self,client):
        c,s=client;assert c.post('/api/reset',json={'confirm':'yes'}).status_code==400
    def test_sec_bad_cik(self,client):
        c,s=client;assert c.get('/api/filings/not-a-cik').status_code==400

class TestAdditionalBoundaries:
    def test_nonfinite_json_rejected(self,client):
        c,s=client;r=c.post('/api/valuation',content='{"fcf":NaN,"growth":0,"discount":0.1,"terminal_growth":0,"years":5,"shares":10}',headers={'Content-Type':'application/json'});assert r.status_code==422
    def test_login_input_not_reflected(self,client):
        c,s=client;r=c.post('/api/login',json={'token':'secret-value','extra':'unexpected'});assert r.status_code==422;assert 'secret-value' not in r.text
    def test_worker_clock(self,client):
        import time
        c,s=client;start=s.s()['index'];r=c.post('/api/config',json={'running':True,'interval_seconds':.5});assert r.status_code==200
        deadline=time.monotonic()+4
        while s.s()['index']==start and time.monotonic()<deadline:time.sleep(.1)
        c.post('/api/config',json={'running':False});assert s.s()['index']>start
    def test_security_headers(self,client):
        c,s=client;r=c.get('/');assert r.headers['X-Frame-Options']=='DENY';assert "object-src 'none'" in r.headers['Content-Security-Policy']
    def test_sec_transport_contract(self,monkeypatch):
        import httpx
        import aegis.connectors as con
        monkeypatch.setenv('SEC_USER_AGENT','Research researcher@example.org');con.SEC_LAST=0
        requests=[]
        def handle(r):
            requests.append(r);return httpx.Response(200,json={'name':'Fixture Issuer','filings':{'recent':{'accessionNumber':['0001'],'form':['10-K'],'filingDate':['2026-01-01'],'acceptanceDateTime':['2026-01-01T18:00:00Z'],'primaryDocument':['annual.htm']}}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:r=con.sec_filings('1045810',client)
        assert requests[0].url.host=='data.sec.gov';assert r['events'][0]['accepted_at']=='2026-01-01T18:00:00Z';assert 'CURRENT_RESEARCH_ONLY' in r['use']
    def test_alpaca_is_paper_read_only(self,monkeypatch):
        import httpx
        from aegis.connectors import alpaca_read
        monkeypatch.setenv('ALPACA_API_KEY','fake');monkeypatch.setenv('ALPACA_SECRET_KEY','fake');requests=[]
        def handle(r):
            requests.append(r);return httpx.Response(200,json=[] if r.url.path.endswith('orders') else {'status':'ACTIVE','equity':'10000','currency':'USD'})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:result=alpaca_read(client)
        assert result['read_only'];assert all(r.method=='GET' and r.url.host=='paper-api.alpaca.markets' for r in requests)

class TestReconciliation:
    def test_fill_cash_and_positions_reconcile(self,svc):
        svc.submit('NVDA','BUY',5,'b');svc.advance();svc.submit('NVDA','SELL',2,'s');svc.advance();r=svc.reconcile();assert r['valid'];assert r['expected_positions']=={'NVDA':3}
    def test_cash_corruption_detected(self,svc):
        with svc.store.tx():s=svc.s();s['cash']='999';svc.store.put('state',s)
        assert not svc.reconcile()['valid']
    def test_order_blocked_after_corruption(self,svc):
        with svc.store.tx():s=svc.s();s['cash']='999';svc.store.put('state',s)
        with pytest.raises(ValueError,match='reconciliation'):svc.submit('NVDA','BUY',1,'fail')
    def test_positions_corruption_detected(self,svc):
        svc.submit('NVDA','BUY',2,'b');svc.advance()
        with svc.store.tx():s=svc.s();s['positions']['NVDA']['qty']=3;svc.store.put('state',s)
        assert not svc.reconcile()['valid']
    def test_api_reconciliation(self,client):
        c,s=client;assert c.get('/api/reconciliation').json()['valid']


class TestAutomatedIntegrity:
    @pytest.mark.parametrize('entrypoint',['cycle','advance','start'])
    @pytest.mark.parametrize('fault',['cash','ledger'])
    def test_all_automated_execution_paths_fail_closed(self,svc,entrypoint,fault):
        if fault=='cash':
            with svc.store.tx():
                s=svc.s();s['cash']='999';svc.store.put('state',s)
        else:
            svc.store.db.execute("UPDATE events SET payload='{}' WHERE seq=1")
        before=svc.s()
        with pytest.raises(ValueError,match='reconciliation'):
            if entrypoint=='start':svc.configure({'running':True,'auto_orders':True})
            else:getattr(svc,entrypoint)()
        assert svc.s()==before
