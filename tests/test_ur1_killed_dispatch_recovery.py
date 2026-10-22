"""U-R1 — 강제종료 dispatch 복구 (M3_WORK_PACKAGES N3).

prepared/sending/acknowledged 각 단계에서 supervisor 프로세스를 실제
SIGKILL 한 뒤 재시작하면:
- prepared (미dispatch)  -> 재시작 시 단 한 번 resume dispatch, 완료.
- sending / acknowledged -> ambiguous + NEEDS_ATTENTION. 재시작 supervisor는
  절대 auto-redispatch 하지 않는다 (M0-09: 실행 불명은 proof로만 exit).
needs_attention exit은 reconcile proof를 가진 경로(cancel)뿐이고,
두 번째 재시작에도 상태는 그대로 보존된다.
"""
from __future__ import annotations

from _p4_support import outcome_completed, rid
from _ur_support import PRINCIPAL, URTestCase


class UR1KilledDispatchRecovery(URTestCase):
    def test_prepared_kill_resumes_and_completes_once(self):
        ids = self.crash_to("prepared")
        sup, fake = self.make_sup(
            steps=[("sleep", 0.6), outcome_completed()])
        # __init__ reconcile -> resume dispatch (STORAGE.md §3 ④)
        self.wait_state(sup, ids["job_id"], ("completed",))
        self.assertEqual(len(fake.started), 1,
                         "resume must dispatch exactly once")
        req = self.request_row(sup, PRINCIPAL, ids["request_id"])
        self.assertEqual(req["phase"], "completed")
        self.assertEqual(len(self.attempts(sup, ids["job_id"])), 1)
        starts = [e for e in self.events(sup, ids["job_id"])
                  if e["to_state"] == "starting"]
        self.assertEqual(len(starts), 1,
                         "no duplicate dispatch in the journal")
        self.assertRegex(self.job_row(sup, ids["job_id"])["base_revision"],
                         r"^[0-9a-f]{40,64}$",
                         "baseline persisted as a resolved sha")
        # durable replay of the SAME request: same receipt, zero extra prompts
        env = sup.handle("worker_start", self.start_args(ids["request_id"]),
                         principal=PRINCIPAL)
        self.assertTrue(env["receipt"]["replayed"])
        self.assertEqual(env["data"]["job"]["job_id"], ids["job_id"])
        self.assertEqual(len(fake.started), 1)
        res = sup.handle("worker_result",
                         {"job_id": ids["job_id"], "section": "summary",
                          "limit": 10},
                         principal=PRINCIPAL)
        self.assertIn("data", res, res.get("error"))

    def test_sending_kill_is_needs_attention_with_zero_redispatch(self):
        ids = self.crash_to("sending")
        sup, fake = self.make_sup()
        job = self.job_row(sup, ids["job_id"])
        self.assertEqual(job["state"], "needs_attention")
        self.assertIn("sending", job["attention_reason"])
        req = self.request_row(sup, PRINCIPAL, ids["request_id"])
        self.assertEqual(req["phase"], "ambiguous")
        att = [a for a in self.attempts(sup, ids["job_id"])
               if a["attempt_id"] == req["attempt_id"]][0]
        self.assertEqual(att["state"], "needs_attention")
        self.assertEqual(fake.started, [],
                         "an unknown dispatch is never auto re-dispatched")
        self.assertFalse(
            sup.journal.claim_dispatch(PRINCIPAL, ids["request_id"]))
        # a new followup is refused until the ambiguity is reconciled
        env = sup.handle("worker_followup", {
            "request_id": rid(), "user_request_ref": "req/2026/ur-1",
            "job_id": ids["job_id"],
            "expected_attempt_id": job["active_attempt_id"],
            "mode": "retry", "message": "retry after crash"},
            principal=PRINCIPAL)
        self.assertEqual(env["error"]["code"], "DISPATCH_AMBIGUOUS")
        self.assertEqual(fake.started, [])
        # the legal exit carries a reconcile proof (state.py): cancel
        env = sup.handle("worker_cancel", {
            "request_id": rid(), "job_id": ids["job_id"],
            "expected_attempt_id": job["active_attempt_id"]},
            principal=PRINCIPAL)
        self.assertIn("data", env, env.get("error"))
        self.wait_state(sup, ids["job_id"], ("cancelled",))
        path = [(e["from_state"], e["to_state"])
                for e in self.events(sup, ids["job_id"])]
        self.assertIn(("needs_attention", "cancel_requested"), path)
        self.assertIn(("cancel_requested", "cancelled"), path)
        self.assertEqual(fake.started, [],
                         "proof-based exit still sent no new prompt")

    def test_acked_kill_survives_second_restart_unchanged(self):
        ids = self.crash_to("acked")
        sup1, fake1 = self.make_sup()
        job1 = self.job_row(sup1, ids["job_id"])
        self.assertEqual(job1["state"], "needs_attention")
        req1 = self.request_row(sup1, PRINCIPAL, ids["request_id"])
        self.assertEqual(req1["phase"], "ambiguous")
        self.assertEqual(fake1.started, [])
        events1 = self.events(sup1, ids["job_id"])
        sup1.close()
        sup2, fake2 = self.make_sup()
        job2 = self.job_row(sup2, ids["job_id"])
        self.assertEqual(job2["state"], "needs_attention")
        self.assertEqual(job2["attention_reason"], job1["attention_reason"],
                         "a second restart must not rewrite the reason")
        req2 = self.request_row(sup2, PRINCIPAL, ids["request_id"])
        self.assertEqual(req2["phase"], "ambiguous")
        self.assertEqual(sup2.store.reconcile_after_restart(), [],
                         "reconcile replay is idempotent")
        self.assertEqual(self.events(sup2, ids["job_id"]), events1)
        self.assertEqual(fake2.started, [],
                         "NEEDS_ATTENTION is never auto-healed or re-run")
