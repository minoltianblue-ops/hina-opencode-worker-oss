import importlib.util
import json
from pathlib import Path
import unittest
FILE=Path(__file__).resolve().parents[1]/'experiments/m4/a3/transport_probe.py'
spec=importlib.util.spec_from_file_location('a3_probe_tests',FILE)
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

class ProbeValidation(unittest.TestCase):
    def envelope(self):
        return {'tool':'worker_catalog','data':{'gate':'PASS','models':[]}}
    def test_structured_catalog(self):
        result=probe.check_catalog({'result':{'structuredContent':self.envelope()}})
        self.assertEqual(result['gate'],'PASS')
    def test_text_catalog_fallback(self):
        result=probe.check_catalog({'result':{'content':[{'type':'text','text':json.dumps(self.envelope())}]}})
        self.assertEqual(result['gate'],'PASS')
    def test_error_is_not_a_pass(self):
        with self.assertRaises(ValueError):
            probe.check_catalog({'result':{'isError':True,'structuredContent':self.envelope()}})
    def test_blocked_catalog_is_not_a_pass(self):
        env=self.envelope();env['data']['gate']='BLOCKED'
        with self.assertRaises(ValueError):
            probe.check_catalog({'result':{'structuredContent':env}})
if __name__=='__main__':unittest.main()
