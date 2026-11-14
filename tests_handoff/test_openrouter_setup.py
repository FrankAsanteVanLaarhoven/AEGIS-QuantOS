"""Offline contract tests. No actual credentials, provider calls or charges."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('aegis_setup', ROOT/'scripts/aegis_openrouter_setup.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
FAKE = 'offline-test-credential-value-only'

class SetupTests(unittest.TestCase):
    def test_rejects_short_secret(self):
        with self.assertRaises(m.SetupError): m.validate_secret('short')
    def test_rejects_whitespace(self):
        with self.assertRaises(m.SetupError): m.validate_secret('a'*20+' '+'b'*20)
    def test_rejects_placeholder(self):
        with self.assertRaises(m.SetupError): m.validate_secret('replace-with-a-long-credential')
    def test_accepts_bounded_secret(self):
        self.assertEqual(m.validate_secret(FAKE), FAKE)
    def test_missing_secret_blocks(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(m.SetupError,'BLOCKED_CREDENTIAL'): m.get_secret('EXAMPLE_KEY')
    def test_ambiguous_secret_blocks(self):
        with patch.dict(os.environ, {'EXAMPLE_KEY':FAKE, 'EXAMPLE_KEY_FILE':'/not-read'}, clear=True):
            with self.assertRaisesRegex(m.SetupError,'AMBIGUOUS'): m.get_secret('EXAMPLE_KEY')
    def test_environment_secret(self):
        with patch.dict(os.environ, {'EXAMPLE_KEY':FAKE}, clear=True):
            self.assertEqual(m.get_secret('EXAMPLE_KEY'),FAKE)
    def test_private_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'secrets/key'; m.write_secret(p,FAKE)
            self.assertEqual(m.read_secret_file(p),FAKE)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'key'; m.write_secret(p,FAKE)
            with self.assertRaisesRegex(m.SetupError,'ALREADY_EXISTS'): m.write_secret(p,FAKE)
    def test_rejects_world_readable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'key'; p.write_text(FAKE); p.chmod(0o644)
            with self.assertRaises(m.SetupError): m.read_secret_file(p)
    def test_rejects_symlink_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'key'; m.write_secret(p,FAKE)
            link=Path(tmp)/'link';link.symlink_to(p)
            with self.assertRaises(m.SetupError): m.read_secret_file(link)
    def test_refuses_git_secret_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git','init','-q',tmp],check=True)
            with self.assertRaisesRegex(m.SetupError,'INSIDE_GIT'): m.write_secret(Path(tmp)/'key',FAKE)
    def test_rejects_management_key_as_runtime(self):
        with self.assertRaisesRegex(m.SetupError,'MANAGEMENT_KEY'):
            m.check_runtime_key(FAKE,transport=lambda *a:{'data':{'is_management_key':True}})
    def test_status_never_returns_key_labels(self):
        result=m.check_runtime_key(FAKE,transport=lambda *a:{'data':{'label':FAKE,'limit':10,'is_management_key':False}})
        self.assertNotIn(FAKE,json.dumps(result))
        self.assertEqual(result['state'],'AUTHENTICATED')
    def test_expired_key_rejected(self):
        with self.assertRaisesRegex(m.SetupError,'EXPIRED'):
            m.check_runtime_key(FAKE,transport=lambda *a:{'data':{'expires_at':'2000-01-01T00:00:00Z'}})
    def test_invalid_limit_blocks_before_network(self):
        for limit in (0,-1,float('nan'),float('inf')):
            with self.subTest(limit=limit), self.assertRaises(m.SetupError):
                m.create_runtime_key(FAKE,Path('/not-created/key'),limit,transport=lambda *a:self.fail('network'))
    def test_invalid_expiry_blocks(self):
        with self.assertRaises(m.SetupError):
            m.create_runtime_key(FAKE,Path('/not-created/key'),1,0,transport=lambda *a:self.fail('network'))
    def test_create_saves_secret_and_lifetime_cap(self):
        calls=[]
        def transport(method,path,key,body=None):
            calls.append((method,path,body))
            return {'key':FAKE,'data':{'hash':'testhash123456'}}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'secrets/key'
            result=m.create_runtime_key(FAKE,p,10,transport=transport)
            self.assertEqual(m.read_secret_file(p),FAKE)
            self.assertEqual(calls[0][2]['limit'],10)
            self.assertIsNone(calls[0][2]['limit_reset'])
            self.assertNotIn(FAKE,json.dumps(result))
            self.assertFalse(p.with_name('key.provision.lock').exists())
    def test_existing_key_prevents_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'key';m.write_secret(p,FAKE)
            with self.assertRaises(m.SetupError):
                m.create_runtime_key(FAKE,p,10,transport=lambda *a:self.fail('network'))
    def test_failed_write_rolls_back_remote_key(self):
        calls=[]
        def transport(method,path,key,body=None):
            calls.append((method,path))
            return {'key':FAKE,'data':{'hash':'testhash123456'}} if method=='POST' else {'data':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'write_secret',side_effect=m.SetupError('LOCAL_WRITE_FAILED')):
            with self.assertRaises(m.SetupError): m.create_runtime_key(FAKE,Path(tmp)/'key',10,transport=transport)
        self.assertEqual(calls,[('POST','/keys'),('DELETE','/keys/testhash123456')])
    def test_catalog_requires_capabilities(self):
        rows={'data':[{'id':'example/one','supported_parameters':['tools','structured_outputs']},
                      {'id':'example/two','supported_parameters':['tools']}]}
        self.assertEqual([x['id'] for x in m.candidate_models(rows)],['example/one'])
    def test_bad_catalog_rejected(self):
        with self.assertRaises(m.SetupError): m.candidate_models({'data':None})
    def test_smoke_requires_explicit_authorization(self):
        with self.assertRaises(m.SetupError):
            m.smoke(FAKE,'example/model',transport=lambda *a:self.fail('network'))
    def test_smoke_rejects_uncapped_key(self):
        with self.assertRaisesRegex(m.SetupError,'CAPPED'):
            m.smoke(FAKE,'example/model',allow_billable=True,transport=lambda *a:{'data':{'limit':None}})
    def test_smoke_rejects_exhausted_key(self):
        with self.assertRaisesRegex(m.SetupError,'EXHAUSTED'):
            m.smoke(FAKE,'example/model',allow_billable=True,transport=lambda *a:{'data':{'limit':10,'limit_remaining':0}})
    def test_smoke_success_and_schema_payload(self):
        payloads=[]
        def transport(method,path,key,body=None):
            if path=='/key':return {'data':{'limit':10,'limit_remaining':9}}
            payloads.append(body)
            return {'model':'example/model','choices':[{'message':{'content':'{"ok":true}'}}], 'usage':{'total_tokens':12}}
        result=m.smoke(FAKE,'example/model',allow_billable=True,transport=transport)
        self.assertEqual(result['state'],'CREDENTIALED_INFERENCE_SMOKE_PASSED')
        self.assertEqual(payloads[0]['response_format']['type'],'json_schema')
        self.assertTrue(payloads[0]['provider']['require_parameters'])
        self.assertTrue(payloads[0]['provider']['zdr'])
        self.assertFalse(payloads[0]['provider']['allow_fallbacks'])
        self.assertIsNone(result['usage']['cost'])
        self.assertNotIn(FAKE,json.dumps(payloads))
    def test_smoke_rejects_schema_violation(self):
        def transport(method,path,key,body=None):
            return {'data':{'limit':10}} if path=='/key' else {'choices':[{'message':{'content':'{"ok":1}'}}]}
        with self.assertRaisesRegex(m.SetupError,'SCHEMA_FAILED'):
            m.smoke(FAKE,'example/model',allow_billable=True,transport=transport)
    def test_endpoint_allowlist(self):
        with self.assertRaisesRegex(m.SetupError,'ALLOWLISTED'):
            m.request_json('POST','https://other.example/api',FAKE)
    def test_redirect_rejected(self):
        with self.assertRaisesRegex(m.SetupError,'REDIRECT_REJECTED'):
            m.NoRedirect().redirect_request(None,None,302,'',{},'https://other.example')
    def test_cli_error_redacted(self):
        with patch.object(m,'get_secret',side_effect=m.SetupError('BLOCKED_CREDENTIAL')),io.StringIO() as buf,contextlib.redirect_stdout(buf):
            code=m.main(['verify'])
            out=buf.getvalue()
        self.assertEqual(code,2)
        self.assertNotIn(FAKE,out)
        self.assertIn('BLOCKED_CREDENTIAL',out)

if __name__=='__main__':unittest.main()
