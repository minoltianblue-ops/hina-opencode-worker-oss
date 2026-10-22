"""T02 — defaults 변경 뒤 replay 원래 intent 유지; followup replay가 busy 선검사에 막히지 않음.

(M1_WORK_PACKAGES.md 필수 자동화 테스트 T02)
"""
from __future__ import annotations

import json

from hina_opencode_worker.requests import BusyError

from tests._support import StoreTestCase


class T02DefaultsAndBusyReplay(StoreTestCase):
    def test_replay_keeps_original_intent_after_defaults_change(self):
        defaults = {
            "model": "opencode/free-A",
            "profile": "inspect",
            "repository_path": "/Users/example/repo-a",
        }
        journal = self.journal(defaults)
        receipt, _ = journal.intake("u1", "req-1", "worker_start", {"goal": "do the thing"})
        job = self.store.get_job(receipt["job_id"])
        self.assertEqual(job["requested_model"], "opencode/free-A")
        self.assertEqual(job["profile"], "inspect")
        self.assertEqual(job["repository_path"], "/Users/example/repo-a")

        # Operator/config defaults move on.
        defaults["model"] = "opencode/free-B"
        defaults["repository_path"] = "/Users/example/repo-b"

        replay, was_replay = journal.intake("u1", "req-1", "worker_start", {"goal": "do the thing"})
        self.assertTrue(was_replay)
        self.assertEqual(replay["job_id"], receipt["job_id"])
        job = self.store.get_job(receipt["job_id"])
        self.assertEqual(job["requested_model"], "opencode/free-A")
        self.assertEqual(job["repository_path"], "/Users/example/repo-a")
        row = self.store.get_request("u1", "req-1")
        normalized = json.loads(row["normalized_json"])
        self.assertEqual(normalized["model"], "opencode/free-A")

        # A NEW request after the defaults change resolves with the NEW defaults.
        r2, _ = journal.intake("u1", "req-2", "worker_start", {"goal": "next task"})
        self.assertEqual(self.store.get_job(r2["job_id"])["requested_model"], "opencode/free-B")

    def test_followup_replay_not_blocked_by_busy_precheck(self):
        journal = self.journal()
        receipt = self.start_job(journal)
        job_id, attempt_id = receipt["job_id"], receipt["attempt_id"]
        v = self.drive_to(job_id, attempt_id, ["starting", "running", "collecting", "completed"], 1)
        self.assertEqual(self.store.get_job(job_id)["state"], "completed")

        followup_payload = {"note": "apply reviewer feedback"}
        fu, replayed = journal.followup_intake("u1", "req-f1", job_id, followup_payload)
        self.assertFalse(replayed)
        self.assertNotEqual(fu["attempt_id"], attempt_id)
        self.assertEqual(self.store.get_job(job_id)["state"], "starting")

        # Job is now busy (attempt 2 active). Replaying the SAME followup
        # request must return the stored receipt, never BUSY.
        replay, was_replay = journal.followup_intake("u1", "req-f1", job_id, followup_payload)
        self.assertTrue(was_replay)
        self.assertEqual(replay["attempt_id"], fu["attempt_id"])

        # A DIFFERENT followup while busy is refused with BUSY (no auto-queue).
        with self.assertRaises(BusyError):
            journal.followup_intake("u1", "req-f2", job_id, {"note": "another push"})

        # And the busy job still has exactly one active attempt (attempt 2).
        active = self.store.active_attempt(job_id)
        self.assertEqual(active["attempt_id"], fu["attempt_id"])
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?", (job_id,)
        ).fetchone()[0], 2)

    def test_followup_requires_explicit_completed_or_failed_job(self):
        journal = self.journal()
        receipt = self.start_job(journal)
        # Job still 'accepted' with an active attempt -> BUSY, not a new attempt.
        with self.assertRaises(BusyError):
            journal.followup_intake("u1", "req-f1", receipt["job_id"], {"note": "x"})
        self.assertEqual(self.store.get_job(receipt["job_id"])["state"], "accepted")


if __name__ == "__main__":
    import unittest

    unittest.main()
