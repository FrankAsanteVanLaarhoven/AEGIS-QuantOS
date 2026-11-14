"""Explicit, bounded, read-only external connectors. No live execution routes."""
from __future__ import annotations
import os, re, time
from datetime import datetime,timezone
import httpx
from .data import digest

SEC_LAST=0.
def connector_status():
    return [
        {'id':'csv','name':'Point-in-time CSV','state':'READY','role':'Historical dataset import'},
        {'id':'fixture','name':'Seeded market replay','state':'READY','role':'SYNTHETIC — offline engineering tests'},
        {'id':'sec','name':'SEC EDGAR','state':'CONFIGURED_NOT_TESTED' if os.getenv('SEC_USER_AGENT') else 'NEEDS_USER_AGENT','role':'Read-only filing discovery; not a news sentiment model'},
        {'id':'alpaca','name':'Alpaca paper account','state':'CONFIGURED_NOT_TESTED' if os.getenv('ALPACA_API_KEY') and os.getenv('ALPACA_SECRET_KEY') else 'NEEDS_CREDENTIALS','role':'Read-only paper account and order reconciliation; external submissions disabled'},
    ]

def sec_filings(cik,client=None):
    global SEC_LAST
    if not re.fullmatch(r'\d{1,10}',str(cik)): raise ValueError('CIK must contain 1–10 digits')
    ua=os.getenv('SEC_USER_AGENT','')
    if '@' not in ua: raise ValueError('Set SEC_USER_AGENT with a real contact name and email')
    now=time.monotonic()
    if now-SEC_LAST<1: raise ValueError('SEC rate limit: retry after one second')
    SEC_LAST=now; url=f'https://data.sec.gov/submissions/CIK{int(cik):010d}.json'
    own=client is None; c=client or httpx.Client(timeout=15,follow_redirects=False)
    try:
        r=c.get(url,headers={'User-Agent':ua,'Accept':'application/json'}); r.raise_for_status()
        if len(r.content)>8_000_000: raise ValueError('SEC response exceeds limit')
        body=r.json(); recent=body.get('filings',{}).get('recent',{}); events=[]
        for i in range(min(30,len(recent.get('accessionNumber',[])))):
            get=lambda k:recent.get(k,[None]*1000)[i]
            events.append({'accession':get('accessionNumber'),'form':get('form'),'filing_date':get('filingDate'),'accepted_at':get('acceptanceDateTime'),'document':get('primaryDocument')})
        return {'name':body.get('name'),'cik':str(cik),'events':events,'source_url':url,'retrieved_at':datetime.now(timezone.utc).isoformat(),'response_sha256':digest(body),'use':'CURRENT_RESEARCH_ONLY; not automatically joined into historical backtests'}
    finally:
        if own:c.close()

def alpaca_read(client=None):
    key=os.getenv('ALPACA_API_KEY'); secret=os.getenv('ALPACA_SECRET_KEY')
    if not key or not secret: raise ValueError('Paper-account credentials are not configured')
    c=client or httpx.Client(timeout=15,follow_redirects=False); own=client is None
    try:
        headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret}; base='https://paper-api.alpaca.markets'
        a=c.get(base+'/v2/account',headers=headers); a.raise_for_status(); orders=c.get(base+'/v2/orders',params={'status':'all','limit':50},headers=headers); orders.raise_for_status()
        account=a.json()
        return {'source':'ALPACA_PAPER','read_only':True,'status':account.get('status'),'equity':account.get('equity'),'buying_power':account.get('buying_power'),'currency':account.get('currency'),'orders':[{k:o.get(k) for k in ['id','client_order_id','symbol','side','qty','filled_qty','status','submitted_at']} for o in orders.json()]}
    finally:
        if own:c.close()
