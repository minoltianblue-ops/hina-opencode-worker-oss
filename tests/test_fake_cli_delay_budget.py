"""Test-only delay budget preserves payload and defaults, without real sleeps."""
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
from _v2_support import FAKE_CLI_SRC

class FakeDelayBudget(unittest.TestCase):
    def run_fake(self, budget):
        payload=bytes(range(100))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'payload';path.write_bytes(payload)
            env={'HINA_FAKE_STDOUT':str(path),'HINA_FAKE_CHUNK_OUT':'7',
                 'HINA_FAKE_DELAY_OUT':'0.005','HINA_FAKE_EXIT':'0'}
            if budget is not None:env['HINA_FAKE_DELAY_CHUNKS']=str(budget)
            out=io.BytesIO();err=io.BytesIO()
            with mock.patch.dict(os.environ,env,clear=True), \
                 mock.patch('sys.stdout',types.SimpleNamespace(buffer=out)), \
                 mock.patch('sys.stderr',types.SimpleNamespace(buffer=err)), \
                 mock.patch('time.sleep') as sleep:
                with self.assertRaises(SystemExit) as ended:
                    exec(compile(FAKE_CLI_SRC,'synthetic_cli','exec'),{})
                self.assertEqual(ended.exception.code,0)
                self.assertEqual(out.getvalue(),payload)
                self.assertEqual(err.getvalue(),b'')
                return sleep.call_count
    def test_default_retains_every_delay(self):self.assertEqual(self.run_fake(None),15)
    def test_bounded_delay_keeps_all_bytes(self):self.assertEqual(self.run_fake(8),8)
    def test_zero_budget_disables_only_delays(self):self.assertEqual(self.run_fake(0),0)

if __name__=='__main__':unittest.main()
