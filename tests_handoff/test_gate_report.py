import hashlib,importlib.util,json,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gate_report',ROOT/'scripts/aegis_gate_report.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
COMMIT='a'*40
PLAN={'tasks':[{'id':'W04','gates':[{'id':'provider','evidence_kind':'external'}]}]}

class Gates(unittest.TestCase):
    def receipt(self,root,**changes):
        r={'gate_id':'provider','commit':COMMIT,'status':'PASS','evidence_kind':'external','exit_code':0,'command':'offline fixture only','observed_at':'2026-11-14T00:00:00Z'}
        r.update(changes);p=root/'receipt.json';raw=json.dumps(r).encode();p.write_bytes(raw)
        return {'commit':COMMIT,'gates':{'provider':{'path':'receipt.json','sha256':hashlib.sha256(raw).hexdigest()}}}
    def test_missing_evidence_not_complete(self):
        self.assertEqual(m.evaluate(PLAN,{},ROOT)['state'],'INCOMPLETE')
    def test_structural_pass(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state=self.receipt(root)
            self.assertEqual(m.evaluate(PLAN,state,root)['state'],'EVIDENCE_INVENTORY_COMPLETE')
    def test_mocks_do_not_satisfy_external(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state=self.receipt(root,evidence_kind='mock')
            self.assertEqual(m.evaluate(PLAN,state,root)['gates'][0]['reason'],'WRONG_EVIDENCE_KIND')
    def test_skip_is_not_pass(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state=self.receipt(root,status='SKIPPED')
            self.assertEqual(m.evaluate(PLAN,state,root)['state'],'INCOMPLETE')
    def test_tampering_detected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state=self.receipt(root);(root/'receipt.json').write_text('{}')
            self.assertEqual(m.evaluate(PLAN,state,root)['gates'][0]['reason'],'HASH_MISMATCH')
    def test_wrong_commit_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);state=self.receipt(root,commit='b'*40)
            self.assertEqual(m.evaluate(PLAN,state,root)['gates'][0]['reason'],'STALE_COMMIT_EVIDENCE')
    def test_parent_path_rejected(self):
        state={'commit':COMMIT,'gates':{'provider':{'path':'../outside','sha256':'0'*64}}}
        self.assertEqual(m.evaluate(PLAN,state,ROOT)['gates'][0]['reason'],'UNSAFE_EVIDENCE_PATH')
