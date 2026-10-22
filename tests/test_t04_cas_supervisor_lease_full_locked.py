"""T04 — CAS 충돌·두 supervisor·workspace lease·DB FULL/locked의 안전한 실패.

(M1_WORK_PACKAGES.md 필수 자동화 테스트 T04)

FULL and LOCKED are produced by SQLite itself (PRAGMA max_page_count cap;
a second connection holding BEGIN IMMEDIATE), never by mocking the driver.
Safe-fail means: typed error, full rollback, committed data intact,
integrity_check ok, and no pretend-success.
"""
from __future__ import annotations

import sqlite3
import unittest

from hina_opencode_worker.store import (
    AlreadyRunningError,
    CasConflictError,
    DatabaseLockedError,
    LeaseHeldError,
    StorageFullError,
    StaleFencingError,
    Store,
    SupervisorLock,
)

from tests._support import PAYLOAD, StoreTestCase


class T04SafeFailures(StoreTestCase):
    # ------------------------------------------------------------------ CAS

    def test_cas_conflict_blocks_stale_writer(self):
        journal = self.journal()
        receipt = self.start_job(journal)
        job_id = receipt["job_id"]
        # Writer A wins with the current version.
        self.store.transition_job(job_id, 1, "starting", "lease-acquired")
        # Writer B still holds version 1 -> CAS conflict, no overwrite.
        with self.assertRaises(CasConflictError) as ctx:
            self.store.transition_job(job_id, 1, "running", "stale-writer")
        self.assertEqual(ctx.exception.actual_version, 2)
        job = self.store.get_job(job_id)
        self.assertEqual(job["state"], "starting")
        self.assertEqual(job["version"], 2)
        # Journal has exactly the creation + one transition row.
        self.assertEqual(
            [e["to_state"] for e in self.store.journal(job_id)],
            ["accepted", "starting"],
        )

    # ------------------------------------------------------- two supervisors

    def test_second_supervisor_cannot_acquire_singleton_lock(self):
        lock_path = self.tmp / "run" / "supervisor.lock"
        first = SupervisorLock(lock_path)
        first.acquire()
        self.addCleanup(first.release)
        second = SupervisorLock(lock_path)
        with self.assertRaises(AlreadyRunningError):
            second.acquire()
        # After the first releases, a new supervisor can take over.
        first.release()
        second.acquire()
        self.addCleanup(second.release)

    # -------------------------------------------------------- workspace lease

    def test_workspace_lease_exclusion_and_fencing(self):
        journal = self.journal()
        r1 = self.start_job(journal)
        token1 = self.store.acquire_workspace_lease(
            canonical_path="/Users/example/fixture",
            job_id=r1["job_id"],
            attempt_id=r1["attempt_id"],
            executor_id="w1",
            git_common_dir="/Users/example/fixture/.git",
            base_revision="deadbeef",
        )
        r2, _ = journal.intake("u1", "req-2", "worker_start", PAYLOAD)
        with self.assertRaises(LeaseHeldError):
            self.store.acquire_workspace_lease(
                canonical_path="/Users/example/fixture",
                job_id=r2["job_id"],
                attempt_id=r2["attempt_id"],
                executor_id="w1",
                git_common_dir="/Users/example/fixture/.git",
                base_revision="deadbeef",
            )
        # A stale supervisor's fencing token is rejected.
        with self.assertRaises(StaleFencingError):
            self.store.assert_fencing("/Users/example/fixture", token1 - 1)
        self.store.assert_fencing("/Users/example/fixture", token1)

        self.store.release_workspace_lease("/Users/example/fixture", token1)
        token2 = self.store.acquire_workspace_lease(
            canonical_path="/Users/example/fixture",
            job_id=r2["job_id"],
            attempt_id=r2["attempt_id"],
            executor_id="w1",
            git_common_dir="/Users/example/fixture/.git",
            base_revision="deadbeef",
        )
        self.assertGreater(token2, token1)
        with self.assertRaises(StaleFencingError):
            self.store.assert_fencing("/Users/example/fixture", token1)

    # -------------------------------------------------------------- DB FULL

    def test_storage_full_refuses_new_intake_atomically(self):
        pages = self.store.diagnostics()["page_count"]
        self.store.set_max_page_count(pages + 1)
        huge = dict(PAYLOAD, goal="x" * 400_000)
        with self.assertRaises(StorageFullError) as ctx:
            self.journal().intake("u1", "req-huge", "worker_start", huge)
        self.assertEqual(ctx.exception.code, "STORAGE_FULL")
        # Atomic refusal: no partial request/job/attempt rows.
        for table in ("requests", "jobs", "attempts", "state_events"):
            self.assertEqual(
                self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                f"{table} left partial rows",
            )
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_storage_full_during_running_transition_keeps_last_state(self):
        journal = self.journal()
        receipt = self.start_job(journal)
        job_id, attempt_id = receipt["job_id"], receipt["attempt_id"]
        v = self.drive_to(job_id, attempt_id, ["starting", "running"], 1)
        pages = self.store.diagnostics()["page_count"]
        self.store.set_max_page_count(pages + 1)
        # Disk-full while trying to mark needs_attention: the marking write
        # fails; the job must stay in its last durable state, not half-moved.
        with self.assertRaises(StorageFullError):
            self.store.transition_job(
                job_id, v, "needs_attention", "disk-full-attempt",
                attention_reason="y" * 400_000,
            )
        job = self.store.get_job(job_id)
        self.assertEqual(job["state"], "running")
        self.assertEqual(job["version"], v)
        self.assertEqual(self.store.integrity_check(), "ok")

    # ------------------------------------------------------------ DB LOCKED

    def test_locked_db_fails_safe_after_busy_timeout(self):
        journal = self.journal()
        receipt = self.start_job(journal)
        job_id = receipt["job_id"]
        blocker = sqlite3.connect(str(self.db))
        blocker.isolation_level = None
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        # Short busy_timeout keeps the test fast; production default is 5000ms.
        challenger = Store.open(self.db, busy_timeout_ms=100)
        try:
            with self.assertRaises(DatabaseLockedError):
                challenger.transition_job(job_id, 1, "starting", "contended")
            self.assertEqual(self.store.get_job(job_id)["state"], "accepted")
            self.assertEqual(self.store.integrity_check(), "ok")
        finally:
            blocker.execute("ROLLBACK")
        # Lock released -> the same writer now succeeds.
        challenger.transition_job(job_id, 1, "starting", "after-lock-release")
        self.assertEqual(self.store.get_job(job_id)["state"], "starting")

    def test_pragma_baseline_matches_contract(self):
        d = self.store.diagnostics()
        self.assertEqual(str(d["journal_mode"]).lower(), "wal")
        self.assertEqual(d["synchronous"], 2)  # FULL
        self.assertEqual(d["foreign_keys"], 1)
        self.assertEqual(d["busy_timeout"], 5000)
        self.assertEqual(d["user_version"], 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
