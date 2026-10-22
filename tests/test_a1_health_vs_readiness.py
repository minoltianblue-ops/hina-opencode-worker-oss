"""T-A1-2: health must NOT be readiness (M4-A1 acceptance).

health = process alive: tunnel endpoint pid + proxy initialize/ping.
The proxy answers ping WITHOUT the supervisor (mcp_proxy.py handle()),
so health stays PASS while the backend is down.

readiness = the full chain reaches backend+model: worker_catalog over
endpoint->proxy->UDS, the pinned OpenCode binary fingerprint (read-only
OBSERVED pin, OPENCODE_V2.md §5), and a catalog model that is
enabled+available — with the LIVE model tier staying dormant
(BLOCKED_NO_APPROVAL): no model call, no user-account action in M4-A1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _p4_support import Clock, FakeBackend, ROOT, TOOLS_JSON
from hina_opencode_worker.supervisor import (ModelCatalog, Supervisor,
                                             SupervisorConfig)

ENDPOINT = ROOT / "experiments" / "m4" / "a1" / "tunnel_endpoint.py"
PROBE = ROOT / "experiments" / "m4" / "a1" / "readiness_probe.py"
OPENCODE_BIN = os.environ.get("OPENCODE_BIN",
                              os.path.expanduser("~/.opencode/bin/opencode"))


class A1ProbeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a1hr_"))
        self.root = self.tmp / "sup"
        self.catalog = ModelCatalog(profiles=("inspect", "develop"),
                                    gate="PASS",
                                    capabilities={"develop": True})
        self.catalog.add("fake/free", "free", probe_status="live_verified")
        self.catalog.add("fake/off", "free", probe_status="unavailable")
        self.sup = Supervisor(SupervisorConfig(root=str(self.root)),
                              backend=FakeBackend(), catalog=self.catalog,
                              clock=Clock().monotonic, time_fn=Clock().time)
        self.sock = str(self.sup.start_uds())
        self.info = self.tmp / "endpoint-info.json"
        self.ep = subprocess.Popen(
            [sys.executable, str(ENDPOINT), "--socket", self.sock,
             "--tools", str(TOOLS_JSON), "--port", "0",
             "--info-file", str(self.info)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH":
                 str(ROOT / "src") + ":" + str(ROOT / "tests")})
        deadline = time.time() + 10
        while not self.info.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.info.exists(), "endpoint never came up")
        self.addCleanup(self._teardown)

    def _teardown(self):
        if self.ep.poll() is None:
            self.ep.kill()
            self.ep.wait(timeout=5)
        self.sup.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def probe(self, mode, extra=(), env_over=None):
        env = {k: v for k, v in os.environ.items()
               if k != "M4_A1_LIVE_MODEL_OK"}
        env.update(env_over or {})
        p = subprocess.run(
            [sys.executable, str(PROBE), mode,
             "--info-file", str(self.info)] + list(extra),
            capture_output=True, text=True, timeout=90, env=env,
            cwd=str(ROOT))
        try:
            doc = json.loads(p.stdout)
        except json.JSONDecodeError:
            doc = {"raw": p.stdout, "stderr": p.stderr[-400:]}
        return p.returncode, doc


class HealthVsReadinessTest(A1ProbeTestCase):
    @unittest.skipUnless(Path(OPENCODE_BIN).exists(),
                         "pinned opencode binary not present on this host")
    def test_health_survives_backend_death_but_readiness_does_not(self):
        rc, doc = self.probe("health")
        self.assertEqual(rc, 0, doc)
        self.assertEqual(doc["verdict"], "HEALTHY")
        self.assertTrue(doc["checks"]["proxy"]["ok"])

        rc, doc = self.probe("readiness", ["--binary", OPENCODE_BIN])
        self.assertEqual(rc, 0, doc)          # supervisor up
        self.assertEqual(doc["verdict"], "READY", doc)
        self.assertTrue(doc["checks"]["supervisor"]["ok"])
        self.assertEqual(doc["checks"]["live_model"]["status"],
                         "BLOCKED_NO_APPROVAL")

        self.sup.close()                      # backend dies; processes
        # health only cares that endpoint+proxy are alive: ping is served
        # by the proxy alone -> health must STILL pass.
        rc, doc = self.probe("health")
        self.assertEqual(rc, 0, doc)
        self.assertEqual(doc["verdict"], "HEALTHY")

        rc, doc = self.probe("readiness")
        self.assertEqual(rc, 1, doc)
        self.assertEqual(doc["verdict"], "NOT_READY")
        self.assertFalse(doc["checks"]["supervisor"]["ok"])
        self.assertIn("supervisor_unreachable",
                      doc["checks"]["supervisor"]["detail"])

    @unittest.skipUnless(Path(OPENCODE_BIN).exists(),
                         "pinned opencode binary not present on this host")
    def test_binary_pin_uses_observed_fingerprint(self):
        rc, doc = self.probe("readiness", ["--binary", OPENCODE_BIN])
        self.assertEqual(rc, 0, doc)
        bp = doc["checks"]["binary_pin"]
        self.assertTrue(bp["ok"], bp)
        self.assertIn("sha_match=True", bp["detail"])

    def test_binary_pin_mismatch_blocks_readiness(self):
        bogus = self.tmp / "bogus-opencode"
        bogus.write_bytes(b"not the pinned binary")
        rc, doc = self.probe("readiness", ["--binary", str(bogus)])
        self.assertEqual(rc, 1, doc)
        self.assertFalse(doc["checks"]["binary_pin"]["ok"])
        self.assertIn("CONTRACT_MISMATCH", doc["checks"]["binary_pin"]
                      ["detail"])

    def test_model_policy_never_counts_unavailable_models(self):
        saved = dict(self.catalog._models)
        self.catalog._models["fake/free"]["probe_status"] = "unavailable"
        try:
            rc, doc = self.probe("readiness")
            self.assertEqual(rc, 1, doc)
            mp = doc["checks"]["model_policy"]
            self.assertFalse(mp["ok"])
            self.assertIn("no enabled available model", mp["detail"])
        finally:
            self.catalog._models = saved

    def test_live_tier_requires_exact_approved_model(self):
        # A model NOT in the catalog must be refused before any exec
        # (D2: the probe never picks a model by itself).
        rc, doc = self.probe("readiness", ["--allow-live"],
                             env_over={"M4_A1_LIVE_MODEL_OK":
                                       "evil/some-model"})
        self.assertEqual(rc, 1, doc)
        lm = doc["checks"]["live_model"]
        self.assertEqual(lm["status"], "MODEL_NOT_ALLOWED")
        # An approved id but no pinned binary -> refused, readiness
        # stays blocked; the live tier never runs a partial command.
        rc, doc = self.probe("readiness", ["--allow-live"],
                             env_over={"M4_A1_LIVE_MODEL_OK":
                                       "fake/free"})
        self.assertEqual(rc, 1, doc)
        lm = doc["checks"]["live_model"]
        self.assertEqual(lm["status"], "BINARY_MISSING")
        # M4-A1 never executes the live tier itself: no account action.


if __name__ == "__main__":
    unittest.main()
