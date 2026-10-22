"""N3 U-R test support: crash-kill + supervisor-restart harness.

Shares one temp root (db/artifacts/keys) between a SIGKILLed child
Supervisor and the restarted in-process Supervisor, so recovery runs
against REAL committed state — same discipline as T03, at the supervisor
level. Fake backend + synthetic fixtures only; nothing external is
touched.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from _p4_support import FakeBackend, ModelCatalog  # noqa: F401 (path setup)
from _wt_support import make_repo
from hina_opencode_worker.supervisor import Supervisor, SupervisorConfig

PRINCIPAL = "local:%d" % os.getuid()


class URTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ur_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp / "repo")
        self.root = self.tmp / "sup"
        self._sups: list[Supervisor] = []

    def tearDown(self):
        # runners must finish before the shared temp root disappears
        # (residual 0): join first, then close.
        for sup in self._sups:
            for t in list(sup._runners.values()):
                t.join(timeout=15)
        for sup in self._sups:
            sup.close()

    # ------------------------------------------------------------- supervisors

    def make_sup(self, *, dispatch_enabled=True, steps=None):
        backend = FakeBackend()
        if steps is not None:
            backend.steps = list(steps)
        catalog = ModelCatalog(profiles=("inspect", "develop"),
                               gate="PASS",
                               capabilities={"develop": True})
        catalog.add("fake/free", "free", probe_status="live_verified")
        config = SupervisorConfig(root=str(self.root),
                                  develop_enforcement_verified=True,
                                  wait_poll_s=0.05,
                                  dispatch_enabled=dispatch_enabled)
        sup = Supervisor(config, backend=backend, catalog=catalog)
        self._sups.append(sup)
        return sup, backend

    def crash_to(self, up_to):
        """SIGKILL a child supervisor at a committed crash phase; returns
        the ids the child emitted before the kill."""
        from _sup_crash_child import crash_to
        return crash_to(self.root, self.repo, up_to, self.tmp)

    # ----------------------------------------------------------------- args

    def start_args(self, request_id):
        # byte-identical to the child's start intent — a replay of the
        # journaled request, not a new one.
        return {"request_id": request_id,
                "user_request_ref": "req/2026/crash-0001",
                "goal": "N3 synthetic crash-path task",
                "workspace": {"repository": str(self.repo),
                              "source_ref": "HEAD"},
                "profile": "inspect", "model": "fake/free",
                "constraints": [], "done_when": ["answer given"]}

    # ---------------------------------------------------------------- reads

    def wait_state(self, sup, job_id, states, timeout=15.0):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = self.job_row(sup, job_id)["state"]
            if last in states:
                return last
            time.sleep(0.03)
        raise AssertionError(
            f"job {job_id} stuck in {last!r}, wanted {states}"
            f"\nevents={self.events(sup, job_id)}")

    def job_row(self, sup, job_id):
        conn = sup._read()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (job_id,)).fetchone()
            assert row is not None, f"job {job_id} missing"
            return dict(row)
        finally:
            conn.close()

    def request_row(self, sup, principal, request_id):
        conn = sup._read()
        try:
            row = conn.execute(
                "SELECT * FROM requests WHERE principal_id = ?"
                " AND request_id = ?", (principal, request_id)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def attempts(self, sup, job_id):
        conn = sup._read()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY ordinal",
                (job_id,))]
        finally:
            conn.close()

    def events(self, sup, job_id):
        conn = sup._read()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT from_state, to_state, reason_code FROM state_events"
                " WHERE job_id = ? ORDER BY seq", (job_id,))]
        finally:
            conn.close()

    def artifact_rows(self, sup, attempt_id):
        conn = sup._read()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM artifacts WHERE attempt_id = ?"
                " ORDER BY relative_path", (attempt_id,))]
        finally:
            conn.close()
