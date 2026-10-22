"""U-R3 — 부분결과 보존 (M3_WORK_PACKAGES N3).

collecting 도중 강제종료: 이미 커밋된 사적 evidence(파일 + registry row)는
재시작 후에도 그대로 남아 있어야 하고, commit되지 않은 result revision은
절대 제공되면 안 된다. 재시작은 dispatch를 재실행하지 않으며
(reconcile = needs_attention), 두 번째 재시작에도 artifact와
state journal이 bit 단위로 동일해야 한다.
"""
from __future__ import annotations

import hashlib

from _ur_support import PRINCIPAL, URTestCase


class UR3PartialPreservation(URTestCase):
    def test_collecting_kill_preserves_evidence_across_restarts(self):
        ids = self.crash_to("collecting")
        sup1, fake1 = self.make_sup()
        job1 = self.job_row(sup1, ids["job_id"])
        self.assertEqual(job1["state"], "needs_attention")
        self.assertIn("acknowledged", job1["attention_reason"])
        req1 = self.request_row(sup1, PRINCIPAL, ids["request_id"])
        self.assertEqual(req1["phase"], "ambiguous")
        self.assertIsNone(job1["result_revision"],
                          "an uncommitted revision must never appear")
        self.assertEqual(fake1.started, [],
                         "recovery must not re-dispatch the unknown run")
        # partial evidence committed before the kill survives it exactly
        rows1 = self.artifact_rows(sup1, ids["attempt_id"])
        self.assertGreaterEqual(len(rows1), 1,
                                "committed partial evidence must survive")
        for row in rows1:
            data = sup1._artifact_store.read_private(row["artifact_id"])
            self.assertEqual(len(data), row["byte_length"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
        # the uncommitted revision is refused, not fabricated
        env = sup1.handle("worker_result",
                          {"job_id": ids["job_id"], "section": "summary",
                           "limit": 10},
                          principal=PRINCIPAL)
        self.assertEqual(env["error"]["code"], "RESULT_NOT_READY")
        events1 = self.events(sup1, ids["job_id"])
        ids1 = [r["artifact_id"] for r in rows1]
        sup1.close()

        sup2, fake2 = self.make_sup()
        self.assertEqual(sup2.store.reconcile_after_restart(), [])
        job2 = self.job_row(sup2, ids["job_id"])
        self.assertEqual(job2["state"], "needs_attention")
        self.assertEqual([r["artifact_id"] for r
                          in self.artifact_rows(sup2, ids["attempt_id"])],
                         ids1, "restart #2 must not move or drop evidence")
        for row in self.artifact_rows(sup2, ids["attempt_id"]):
            data = sup2._artifact_store.read_private(row["artifact_id"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
        self.assertEqual(self.events(sup2, ids["job_id"]), events1,
                         "idempotent replay: no journal churn on restart")
        self.assertEqual(sup2.store.integrity_check(), "ok")
        self.assertEqual(fake2.started, [])
        env = sup2.handle("worker_result",
                          {"job_id": ids["job_id"], "section": "summary",
                           "limit": 10},
                          principal=PRINCIPAL)
        self.assertEqual(env["error"]["code"], "RESULT_NOT_READY")
