"""M4-A2 (WB): managed supervisor run — crash restart, log retention,
observe-only viewer wiring. T-A2-1..4.

Subprocess-level: the wrapper is driven against scripted dummy children
(crash/give-up/verbosity) and, for the viewer wiring, a real in-process
Supervisor (P4 support, fake backend — zero real prompts).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPERVISE = ROOT / "experiments" / "m4" / "a2" / "supervise_run.py"

sys.path.insert(0, str(ROOT / "tests"))
from _p4_support import FakeBackend  # noqa: E402


DUMMY = r"""
import os, sys, time
mode = os.environ["A2_DUMMY"]
cf = os.environ["A2_COUNT"]
n = int(open(cf).read()) if os.path.exists(cf) else 0
open(cf, "w").write(str(n + 1))
if mode == "crashonce":
    if n == 0:
        sys.exit(3)
    print("gen1-up", flush=True)
    time.sleep(60)
elif mode == "alwaysfail":
    sys.exit(1)
elif mode == "chatty":
    for _ in range(600):
        print("X" * 100, flush=True)
    time.sleep(60)
"""


class WrapperBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a2wr_"))
        self.root = self.tmp / "sup"
        self.dummy = self.tmp / "dummy.py"
        self.dummy.write_text(DUMMY)
        self.count = self.tmp / "count"

    def tearDown(self):
        if getattr(self, "proc", None) and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wrapper(self, *extra):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.Popen(
            [sys.executable, str(SUPERVISE), "run", "--root", str(self.root),
             "--child-cmd",
             f"{sys.executable} {self.dummy}"] + list(extra),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    def wait_events(self, needle, timeout=15.0):
        ev = self.root / "logs" / "run-events.log"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if needle in ev.read_text():
                    return True
            except FileNotFoundError:
                pass
            time.sleep(0.1)
        self.fail(f"run-events never contained {needle!r}: "
                  f"{ev.read_text() if ev.exists() else '(none)'}")

    def stop(self, proc, expect=0):
        proc.terminate()
        self.assertEqual(proc.wait(timeout=15), expect)


class TestCrashRestart(WrapperBase):
    """T-A2-1: unexpected child exit restarts; operator SIGTERM does
    not; wrapper exits 0 with no residual child."""

    def test_crash_then_operator_stop(self):
        env = dict(os.environ, A2_DUMMY="crashonce",
                   A2_COUNT=str(self.count), PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.Popen(
            [sys.executable, str(SUPERVISE), "run", "--root", str(self.root),
             "--child-cmd", f"{sys.executable} {self.dummy}",
             "--backoff-base", "0.2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.proc = proc
        try:
            self.wait_events("child_exit rc=3")
            self.wait_events("restart in")
            end = time.monotonic() + 15
            while time.monotonic() < end:
                if int(self.count.read_text()) >= 2:
                    break
                time.sleep(0.1)
            self.assertEqual(int(self.count.read_text()), 2,
                             "exactly one restart happened")
            outlog = self.root / "logs" / "supervisor.out.log"
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if outlog.exists() and "gen1-up" in outlog.read_text():
                    break
                time.sleep(0.1)
            self.assertIn("gen1-up", outlog.read_text(),
                          "restarted child's stdout not retained")
            self.stop(proc)
            self.wait_events("operator_stop")
            # child from the log-pump gen is gone with the wrapper
            left = subprocess.run(
                ["pgrep", "-f", str(self.dummy)],
                capture_output=True, text=True)
            self.assertEqual(left.returncode, 1,
                             f"residual dummy child: {left.stdout}")
        finally:
            if proc.poll() is None:
                proc.kill()


class TestGiveUp(WrapperBase):
    """T-A2-2: crash storm -> wrapper gives up (exit 70) so an outer
    supervisor throttles; no hot loop."""

    def test_fast_crash_give_up(self):
        env = dict(os.environ, A2_DUMMY="alwaysfail",
                   A2_COUNT=str(self.count), PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.Popen(
            [sys.executable, str(SUPERVISE), "run", "--root", str(self.root),
             "--child-cmd", f"{sys.executable} {self.dummy}",
             "--max-fast", "3", "--min-up-s", "0.5",
             "--backoff-base", "0.05", "--backoff-cap", "0.2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.proc = proc
        self.assertEqual(proc.wait(timeout=20), 70)
        self.wait_events("give_up after 3 consecutive fast crashes")
        self.assertEqual(int(self.count.read_text()), 3)


class TestLogRetention(WrapperBase):
    """T-A2-3: child verbosity is retained under a size cap with a
    bounded file count."""

    def test_rotation_cap(self):
        env = dict(os.environ, A2_DUMMY="chatty", A2_COUNT=str(self.count),
                   PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.Popen(
            [sys.executable, str(SUPERVISE), "run", "--root", str(self.root),
             "--child-cmd", f"{sys.executable} {self.dummy}",
             "--log-max-bytes", "4096", "--log-keep", "3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.proc = proc
        try:
            logdir = self.root / "logs"
            end = time.monotonic() + 15
            files = []
            while time.monotonic() < end:
                files = sorted(logdir.glob("supervisor.out.log*"))
                if len(files) >= 2:
                    break
                time.sleep(0.1)
            self.assertGreaterEqual(len(files), 2, "no rotation observed")
            self.assertLessEqual(len(files), 3, "retention cap exceeded")
            for f in files:
                self.assertLessEqual(f.stat().st_size, 4096 + 200,
                                     f.name)
            # newest file holds the newest lines
            self.assertIn("XXXX", files[0].read_text())
        finally:
            self.stop(proc)


class TestViewerWiring(unittest.TestCase):
    """T-A2-4: status/viewer round-trip the real supervisor; the wired
    path is observe-only (mutations refused server-side); unreachable
    supervisor -> NOT_READY exit 1."""

    def setUp(self):
        from hina_opencode_worker.supervisor import (
            ModelCatalog, Supervisor, SupervisorConfig)
        self.tmp = Path(tempfile.mkdtemp(prefix="a2vw_"))
        cat = ModelCatalog(profiles=("inspect", "develop"), gate="PASS")
        cat.add("fake/free", "free", probe_status="live_verified")
        self.sup = Supervisor(
            SupervisorConfig(root=str(self.tmp), wait_poll_s=0.05),
            backend=FakeBackend(), catalog=cat)
        self.sock = self.sup.start_uds()

    def tearDown(self):
        self.sup.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        return subprocess.run([sys.executable, str(SUPERVISE)]
                              + list(args), capture_output=True,
                              text=True, env=env, timeout=30)

    def test_status_ready_then_not_ready(self):
        r = self._run("status", "--root", str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ready"])
        self.sup.close()
        r = self._run("status", "--root", str(self.tmp))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["reason"],
                         "supervisor_unreachable")

    def test_viewer_wiring_surface(self):
        r = self._run("viewer", "--root", str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("current_job", r.stdout)
        # observe-only: the wired viewer path never reaches mutation —
        # proved from the supervisor side, not by trusting the client
        env = self.sup.dispatch({"client": "viewer",
                                 "method": "worker_start",
                                 "arguments": {}},
                                principal=f"local:{os.getuid()}")
        self.assertEqual(env["error"]["code"], "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
