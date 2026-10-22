"""T01 — 같은 request replay는 prompt 1회; 같은 ID 다른 payload/다른 tool conflict.

(M1_WORK_PACKAGES.md 필수 자동화 테스트 T01)
"""
from __future__ import annotations

import json

from hina_opencode_worker.requests import (
    IdempotencyConflictError,
    canonical_json,
    payload_sha256,
)

from tests._support import PAYLOAD, StoreTestCase


class T01ReplayConflict(StoreTestCase):
    def test_replay_same_id_prompts_once(self):
        journal = self.journal()
        receipt, replayed = journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        self.assertFalse(replayed)
        job_id, attempt_id = receipt["job_id"], receipt["attempt_id"]

        prompts: list[str] = []
        journal.mark_prepared("u1", "req-1")
        if journal.claim_dispatch("u1", "req-1"):
            prompts.append("prompt")
        self.assertEqual(len(prompts), 1)

        # Replay: same principal, same request_id, same payload.
        replay, was_replay = journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        self.assertTrue(was_replay)
        self.assertEqual(replay["job_id"], job_id)
        self.assertEqual(replay["attempt_id"], attempt_id)
        self.assertEqual(replay["payload_sha256"], receipt["payload_sha256"])

        # Replay must not open a second dispatch window.
        self.assertFalse(journal.claim_dispatch("u1", "req-1"))
        self.assertEqual(len(prompts), 1)

        row = self.store.get_request("u1", "req-1")
        self.assertEqual(row["phase"], "sending")
        jobs = self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(jobs, 1)

    def test_same_id_different_payload_conflicts(self):
        journal = self.journal()
        journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        changed = dict(PAYLOAD, goal="Different intent entirely")
        with self.assertRaises(IdempotencyConflictError):
            journal.intake("u1", "req-1", "worker_start", changed)
        # Original row untouched.
        row = self.store.get_request("u1", "req-1")
        self.assertEqual(row["payload_sha256"], payload_sha256(canonical_json(PAYLOAD)))

    def test_same_id_different_tool_conflicts(self):
        journal = self.journal()
        receipt, _ = journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        # Same (principal, request_id) reused with worker_followup -> conflict.
        with self.assertRaises(IdempotencyConflictError):
            journal.followup_intake("u1", "req-1", receipt["job_id"], {"note": "x"})
        self.assertEqual(self.store.get_request("u1", "req-1")["tool"], "worker_start")

    def test_different_principal_same_request_id_is_independent(self):
        journal = self.journal()
        r1, _ = journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        r2, replayed = journal.intake("u2", "req-1", "worker_start", PAYLOAD)
        self.assertFalse(replayed)
        self.assertNotEqual(r1["job_id"], r2["job_id"])

    def test_receipt_is_durable_and_replay_returns_stored_receipt(self):
        journal = self.journal()
        receipt, _ = journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        row = self.store.get_request("u1", "req-1")
        stored = json.loads(row["receipt_json"])
        self.assertEqual(stored["job_id"], receipt["job_id"])
        replay, _ = journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        self.assertEqual(replay["job_id"], stored["job_id"])

    def test_acknowledged_request_cannot_reclaim_dispatch(self):
        journal = self.journal()
        journal.intake("u1", "req-1", "worker_start", PAYLOAD)
        journal.mark_prepared("u1", "req-1")
        self.assertTrue(journal.claim_dispatch("u1", "req-1"))
        journal.mark_acknowledged("u1", "req-1")
        self.assertFalse(journal.claim_dispatch("u1", "req-1"))


if __name__ == "__main__":
    import unittest

    unittest.main()
