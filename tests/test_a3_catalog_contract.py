"""Closure regressions: real A3 wiring against frozen schemas, no inference."""
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from _p4_support import (FakeBackend, Supervisor, SupervisorConfig, ModelCatalog,
                         assert_envelope, output_schema, validate_against)
from hina_opencode_worker.supervisor import LiveBackend

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('a3_live_contract',ROOT/'experiments/m4/a3/live_entry.py')
ENTRY=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(ENTRY)
PROBE=types.SimpleNamespace(version='opencode v2.0.30',sha256='d'*64,mapping_revision='test-pin')

class CatalogContract(unittest.TestCase):
    def catalog(self,model='opencode-go/muse-spark-1.3-contributor'):
        with patch('hina_opencode_worker.opencode_v2.probe',return_value=PROBE):
            return ENTRY.build_live_backend('/not-executed','http://127.0.0.1:49374',model,'unverified')[1]

    def test_live_catalog_boolean_capabilities(self):
        cat=self.catalog()
        self.assertTrue(cat.capabilities)
        self.assertTrue(all(type(value) is bool for value in cat.capabilities.values()))

    def test_unclassified_sut_never_claimed_free(self):
        for model in ('opencode-go/muse-spark-1.3-contributor','provider/arbitrary'):
            cat=self.catalog(model)
            self.assertEqual(cat.rows()[0]['billing_class'],'unknown')
            self.assertEqual(cat.rows()[0]['id'],model)
            self.assertEqual(cat.rows()[0]['probe_status'],'unverified')

    def test_actual_entry_catalog_full_output_schema(self):
        with tempfile.TemporaryDirectory() as root:
            sup=Supervisor(SupervisorConfig(root=root,profiles=('inspect',)),
                           backend=FakeBackend(),catalog=self.catalog())
            try:assert_envelope(self,sup.handle('worker_catalog',{'refresh':False},principal='local:test'), 'worker_catalog')
            finally:sup.close()

    def test_typed_additional_properties_validator_rejects_object(self):
        schema=output_schema('worker_catalog')['properties']['data']['properties']['capabilities']
        self.assertTrue(validate_against(schema,{'inspect':{'enforced':True}}))
        self.assertEqual(validate_against(schema,{'inspect':True,'approval_channel':False}),[])

    def test_catalog_rejects_non_boolean_capability_configuration(self):
        for value in ({'enforced':True},1,'yes',None):
            with self.assertRaises(ValueError):ModelCatalog(capabilities={'inspect':value})

    def test_live_backend_refresh_uses_actual_probe_result_fields(self):
        backend=LiveBackend(binary='/not-executed',server_url='http://127.0.0.1:49374',adapter=None)
        with patch.object(backend,'_probe',return_value=PROBE):
            actual=backend.probe()
        self.assertEqual(actual['binary_sha256'],PROBE.sha256)
        self.assertEqual(actual['version'],PROBE.version)

if __name__=='__main__':unittest.main()
