"""R1 F1 regression — active_attempt_id는 같은 job의 attempt만 가리킬 수 있다.

(STORAGE.md §7 앱 트랜잭션 불변식; FK는 존재성만 검사하므로 job_id 대조가 필수.
재현 근거: m1-r1 docs/reviews/m1/p1-0d06604/cross-job-active-attempt.log)
"""
from __future__ import annotations

from hina_opencode_worker.state import InvariantFailureError
from hina_opencode_worker.store import utc_now

from tests._support import PAYLOAD, StoreTestCase


class F1ActiveAttemptOwnership(StoreTestCase):
    def _two_jobs(self):
        journal = self.journal()
        first, _ = journal.intake("u1", "req-first", "worker_start", PAYLOAD)
        second, _ = journal.intake("u1", "req-second", "worker_start", PAYLOAD)
        return journal, first, second

    def test_cross_job_attempt_rejected_pointer_and_version_unchanged(self):
        _, first, second = self._two_jobs()
        before = self.store.get_job(first["job_id"])
        self.assertEqual(before["active_attempt_id"], first["attempt_id"])

        with self.assertRaises(InvariantFailureError):
            with self.store.tx() as conn:
                self.store.set_active_attempt(
                    conn,
                    first["job_id"],
                    second["attempt_id"],
                    before["version"],
                    utc_now(),
                )

        after = self.store.get_job(first["job_id"])
        self.assertEqual(after["active_attempt_id"], first["attempt_id"])
        self.assertEqual(after["version"], before["version"])
        owner = self.store.conn.execute(
            "SELECT job_id FROM attempts WHERE attempt_id = ?",
            (after["active_attempt_id"],),
        ).fetchone()
        self.assertEqual(owner["job_id"], first["job_id"])

    def test_missing_attempt_rejected(self):
        _, first, _ = self._two_jobs()
        before = self.store.get_job(first["job_id"])
        with self.assertRaises(InvariantFailureError):
            with self.store.tx() as conn:
                self.store.set_active_attempt(
                    conn,
                    first["job_id"],
                    "att-does-not-exist",
                    before["version"],
                    utc_now(),
                )
        after = self.store.get_job(first["job_id"])
        self.assertEqual(after["active_attempt_id"], first["attempt_id"])
        self.assertEqual(after["version"], before["version"])

    def test_same_job_attempt_still_accepted(self):
        _, first, _ = self._two_jobs()
        before = self.store.get_job(first["job_id"])
        with self.store.tx() as conn:
            new_version = self.store.set_active_attempt(
                conn,
                first["job_id"],
                first["attempt_id"],
                before["version"],
                utc_now(),
            )
        self.assertEqual(new_version, before["version"] + 1)
        after = self.store.get_job(first["job_id"])
        self.assertEqual(after["active_attempt_id"], first["attempt_id"])
        self.assertEqual(after["version"], before["version"] + 1)
