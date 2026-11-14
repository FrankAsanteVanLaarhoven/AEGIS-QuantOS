#!/usr/bin/env python3
"""Explicitly install the pinned official Kronos source and immutable weights.
Never invoked automatically by the web server. Requires network access.
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from aegis.kronos_adapter import COMMIT

def run(args,cwd=None):subprocess.run(args,cwd=cwd,check=True)
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',choices=['mini','small','base'],default='mini');parser.add_argument('--model-revision');parser.add_argument('--tokenizer-revision');parser.add_argument('--skip-dependencies',action='store_true');args=parser.parse_args()
    if not args.skip_dependencies:run([sys.executable,'-m','pip','install','-r',str(ROOT/'requirements-kronos.txt')])
    from huggingface_hub import HfApi, snapshot_download
    source=ROOT/'vendor'/'Kronos';source.parent.mkdir(exist_ok=True)
    if not (source/'.git').exists():
        if source.exists() and any(source.iterdir()):raise SystemExit('Refusing to overwrite a nonempty non-git vendor/Kronos directory.')
        run(['git','clone','https://github.com/shiyu-coder/Kronos.git',str(source)])
    else:
        dirty=subprocess.check_output(['git','status','--porcelain'],cwd=source,text=True).strip()
        if dirty:raise SystemExit('Refusing to overwrite modified Kronos source.')
        run(['git','fetch','origin',COMMIT],source)
    run(['git','checkout','--detach',COMMIT],source)
    actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    if actual!=COMMIT:raise SystemExit('Source revision verification failed')
    models=Path(os.environ.get('AEGIS_MODELS_DIR',str(ROOT/'models')));models.mkdir(parents=True,exist_ok=True)
    model_repo='NeoQuasar/Kronos-'+args.model;tok_repo='NeoQuasar/Kronos-Tokenizer-'+('2k' if args.model=='mini' else 'base');hub=HfApi()
    revision=hub.model_info(model_repo,revision=args.model_revision or 'main').sha
    tok_revision=hub.model_info(tok_repo,revision=args.tokenizer_revision or 'main').sha
    for repo,rev,name in [(model_repo,revision,'model'),(tok_repo,tok_revision,'tokenizer')]:
        folder=models/name;folder.mkdir(exist_ok=True)
        snapshot_download(repo_id=repo,revision=rev,local_dir=folder,allow_patterns=['config.json','model.safetensors','README.md','LICENSE','LICENSE.*'])
        for f in ('config.json','model.safetensors'):
            if not (folder/f).exists():raise SystemExit(f'{repo}: required SafeTensors artifact missing: {f}')
    src={str(p.relative_to(source)):sha(p) for p in source.rglob('*.py') if '.git' not in p.parts}
    for name in ('LICENSE','README.md'):
        if (source/name).exists():src[name]=sha(source/name)
    weights={str(p.relative_to(models)):sha(p) for name in ('model','tokenizer') for p in (models/name).rglob('*') if p.is_file() and '.cache' not in p.parts}
    manifest={'source_repository':'https://github.com/shiyu-coder/Kronos','source_commit':COMMIT,'model_repo':model_repo,'model_revision':revision,'tokenizer_repo':tok_repo,'tokenizer_revision':tok_revision,'max_context':2048 if args.model=='mini' else 512,'source':src,'weights':weights,'smoke_passed':False}
    tmp=models/'kronos.lock.json.tmp';tmp.write_text(json.dumps(manifest,indent=2));tmp.replace(models/'kronos.lock.json')
    print('Pinned source and weights installed; SHA-256 checks run before model loading.')
    print('Next: python scripts/kronos_smoke.py')
if __name__=='__main__':main()
