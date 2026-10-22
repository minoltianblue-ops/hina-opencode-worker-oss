"""T-A1-3: readiness gate blocks until ready — proof by probe log.

`readiness_probe.py gate` must:
- keep returning NOT_READY (exit 1 after timeout) while the backend is
  absent, logging every attempt (redacted) — a blocked probe is an
  audit trail, not a silent retry;
- flip to READY (exit 0) once the supervisor appears, WITHOUT ever
  starting a job: readiness is read-only (worker_catalog only;
  FakeBackend.start call count stays 0);
- never auto-create work or pick a model (D2/T17): nothing in the
  gate path touches the backend write surface.
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


class CountingBackend(FakeBackend):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.start_calls = 0

    def start(self, **kw):
        self.start_calls += 1
        return super().start(**kw)


class BlockedUntilReadyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a1gate_"))
        self.sock = str(self.tmp / "run" / "supervisor.sock")
        self.info = self.tmp / "endpoint-info.json"
        self.gate_log = self.tmp / "gate-log.jsonl"
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
        self.assertTrue(self.info.exists())
        self.backend = CountingBackend()
        self.sup = None
        self.addCleanup(self._teardown)

    def _teardown(self):
        if self.ep.poll() is None:
            self.ep.kill()
            self.ep.wait(timeout=5)
        if self.sup is not None:
            self.sup.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_supervisor(self):
        catalog = ModelCatalog(profiles=("inspect", "develop"),
                               gate="PASS",
                               capabilities={"develop": True})
        catalog.add("fake/free", "free", probe_status="live_verified")
        (self.tmp / "run").mkdir(parents=True, exist_ok=True)
        cfg = SupervisorConfig(root=str(self.tmp / "sup"))
        # bind the UDS at the exact path the endpoint was told to use
        cfg.sock_path = self.sock
        cfg.lock_path = str(self.tmp / "run" / "supervisor.lock")
        self.sup = Supervisor(cfg, backend=self.backend, catalog=catalog,
                              clock=Clock().monotonic,
                              time_fn=Clock().time)
        self.sup.start_uds()

    def _gate(self, timeout, interval=0.15):
        env = {k: v for k, v in os.environ.items()
               if k != "M4_A1_LIVE_MODEL_OK"}
        p = subprocess.run(
            [sys.executable, str(PROBE), "gate",
             "--info-file", str(self.info),
             "--timeout", str(timeout), "--interval", str(interval),
             "--log-file", str(self.gate_log)],
            capture_output=True, text=True, timeout=timeout + 60,
            cwd=str(ROOT), env=env)
        return p.returncode, json.loads(p.stdout)

    def _attempts(self):
        return [json.loads(l) for l in
                self.gate_log.read_text(encoding="utf-8").splitlines()]

    def test_gate_times_out_blocked_and_logs_every_attempt(self):
        rc, summary = self._gate(timeout=2.0)
        self.assertEqual(rc, 1)
        self.assertEqual(summary["verdict"], "NOT_READY")
        attempts = self._attempts()
        self.assertGreaterEqual(summary["attempts"], 3)
        self.assertEqual(len(attempts), summary["attempts"])
        for a in attempts:
            self.assertEqual(a["verdict"], "NOT_READY")
            self.assertFalse(a["checks"]["supervisor"]["ok"])
            self.assertIn("supervisor_unreachable",
                          a["checks"]["supervisor"]["detail"])
        self.assertEqual(self.backend.start_calls, 0)

    @unittest.skipUnless(Path(OPENCODE_BIN).exists(),
                         "pinned opencode binary not present on this host")
    def test_gate_flips_to_ready_without_starting_anything(self):
        proc = subprocess.Popen(
            [sys.executable, str(PROBE), "gate",
             "--info-file", str(self.info), "--binary", OPENCODE_BIN,
             "--timeout", "20", "--interval", "0.15",
             "--log-file", str(self.gate_log)],
            cwd=str(ROOT), stdout=subprocess.PIPE, text=True,
            env={k: v for k, v in os.environ.items()
                 if k != "M4_A1_LIVE_MODEL_OK"})
        time.sleep(1.0)                    # several blocked attempts first
        self._start_supervisor()
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        attempts = self._attempts()
        verdicts = [a["verdict"] for a in attempts]
        self.assertEqual(verdicts[0], "NOT_READY")
        self.assertEqual(verdicts[-1], "READY")
        first_ready = verdicts.index("READY")
        self.assertTrue(all(v == "NOT_READY" for v in verdicts[:first_ready]))
        self.assertGreater(first_ready, 0)   # it BLOCKED before it passed
        ready = attempts[-1]
        self.assertTrue(ready["checks"]["supervisor"]["ok"])
        self.assertTrue(ready["checks"]["model_policy"]["ok"])
        self.assertEqual(ready["checks"]["live_model"]["status"],
                         "BLOCKED_NO_APPROVAL")
        self.assertEqual(self.backend.start_calls, 0,
                         "readiness gate must never start work")


if __name__ == "__main__":
    unittest.main()
