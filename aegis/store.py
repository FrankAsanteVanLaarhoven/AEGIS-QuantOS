"""Atomic SQLite state and hash-linked evidence. This is tamper-evident, not immutable."""
from __future__ import annotations
import json, sqlite3, threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from .data import canonical, digest

class Store:
    def __init__(self,path):
        self.path=str(path); self.lock=threading.RLock()
        if self.path!=':memory:': Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000; CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, wall_time TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,prev TEXT NOT NULL,hash TEXT NOT NULL); CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL);')
    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self
                if self.db.in_transaction:
                    self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise
    def get(self,key,default=None):
        with self.lock:
            r=self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
            return json.loads(r[0]) if r else default
    def put(self,key,value):
        with self.lock:
            self.db.execute('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value)))
    def append(self,kind,payload):
        with self.lock:
            last=self.db.execute('SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            seq=last['seq']+1 if last else 1; prev=last['hash'] if last else '0'*64
            stamp=datetime.now(timezone.utc).isoformat(); body={'seq':seq,'wall_time':stamp,'kind':kind,'payload':payload,'prev':prev}; h=digest(body)
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(seq,stamp,kind,canonical(payload),prev,h))
            self.put('ledger_head',{'seq':seq,'hash':h})
            return {**body,'hash':h}
    def events(self,limit=200):
        with self.lock:
            rows=self.db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT ?',(limit,)).fetchall()
            return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]
    def verify(self):
        with self.lock:
            prev='0'*64; count=0
            for r in self.db.execute('SELECT * FROM events ORDER BY seq'):
                count+=1; b={k:r[k] for k in ['seq','wall_time','kind','prev']}; b['payload']=json.loads(r['payload'])
                if r['seq']!=count or r['prev']!=prev or digest(b)!=r['hash']: return {'valid':False,'events':count,'error':'Chain mismatch','failed_seq':r['seq']}
                prev=r['hash']
            head=self.get('ledger_head',{'seq':0,'hash':'0'*64})
            return {'valid':head=={'seq':count,'hash':prev},'events':count,'head':prev,'limitation':'A database administrator can rewrite both the chain and its local head. Export and separately retain anchors.'}
    def save_dataset(self,dataset):
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO datasets VALUES(?,?)',(dataset.id,canonical({'rows':dataset.to_rows(),'label':dataset.label,'source':dataset.source,'raw_hash':dataset.raw_hash})))
    def load_dataset(self,id):
        from .data import Dataset
        with self.lock:
            row=self.db.execute('SELECT body FROM datasets WHERE id=?',(id,)).fetchone()
            if not row: raise ValueError('Dataset not found')
            return Dataset(**json.loads(row[0]))
    def result(self,kind,body):
        with self.lock:
            ident=digest({'kind':kind,'body':body})[:20]
            self.db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?)',(ident,kind,canonical(body)))
            return {'id':ident,**body}
    def results(self,kind=None):
        with self.lock:
            rows=self.db.execute('SELECT * FROM results WHERE kind=? ORDER BY rowid DESC LIMIT 20',(kind,)).fetchall() if kind else self.db.execute('SELECT * FROM results ORDER BY rowid DESC LIMIT 20').fetchall()
            return [{'id':r['id'],'kind':r['kind'],'body':json.loads(r['body'])} for r in rows]
