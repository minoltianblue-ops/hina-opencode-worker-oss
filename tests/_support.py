"""Shared helpers for P1 tests."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hina_opencode_worker.requests import RequestJournal
from hina_opencode_worker.store import Store

PAYLOAD = {
    "goal": "Fix the synthetic calculator and add validation",
    "profile": "develop",
    "model": "opencode/glm-5.3-flash-free",
    "workspace": {"repository": "/Users/example/fixture", "source_ref": "HEAD"},
}


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hina-p1-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "jobs.sqlite"
        self.store = Store.open(self.db)
        self.addCleanup(self.store.close)

    def journal(self, defaults: dict | None = None) -> RequestJournal:
        return RequestJournal(self.store, defaults)

    def drive_to(self, job_id: str, attempt_id: str, states: list[str], start_version: int) -> int:
        """Advance job (and attempt) through a legal state path with CAS."""
        v = start_version
        for s in states:
            v = self.store.transition_job(job_id, v, s, f"drive:{s}", attempt_id=attempt_id)
            self.store.update_attempt_state(attempt_id, s)
        return v

    def start_job(self, journal: RequestJournal, request_id: str = "req-start-1"):
        receipt, replayed = journal.intake("u1", request_id, "worker_start", PAYLOAD)
        self.assertFalse(replayed)
        return receipt
