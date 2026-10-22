"""U-R2 — 중복 suppression / durable journal replay (M3_WORK_PACKAGES N3).

같은 request_id의 replay는 restart 전후 모두 저장된 receipt를 반환하고
prompt를 늘리지 않는다. prepared로 살아남은 request는 replay가 resume의
대체 경로가 되지만 prepared->sending CAS 덕분에 dispatch는 정확히 한 번.
ambiguous로 reconcile된 request의 replay와 다른 payload conflict는
어느 쪽에서도 dispatch를 만들지 않는다. followup resume도 중복 attempts 0.
"""
from __future__ import annotations

from _p4_support import outcome_completed, rid
from _ur_support import PRINCIPAL, URTestCase


class UR2DuplicateSuppression(URTestCase):
    def test_prepared_kill_replay_never_duplicates_then_resumes_once(self):
        ids = self.crash_to("prepared")
        # dispatch disabled: neither restart-resume nor client replay may
        # dispatch; the journal row stays durable for a later recovery.
        sup, fake = self.make_sup(dispatch_enabled=False)
        self.assertEqual(self.request_row(sup, PRINCIPAL,
                                          ids["request_id"])["phase"],
                         "prepared")
        self.assertEqual(fake.started, [])
        # same request_id, different payload -> conflict, no new job
        changed = dict(self.start_args(ids["request_id"]),
                       goal="a different payload entirely")
        env = sup.handle("worker_start", changed, principal=PRINCIPAL)
        self.assertEqual(env["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.attempts(sup, ids["job_id"])), 1)
        self.assertEqual(self.request_row(sup, PRINCIPAL,
                                          ids["request_id"])["phase"],
                         "prepared")
        # exact replay (twin calls) returns the stored receipt and
        # dispatches nothing while dispatch is off
        fake.steps = [outcome_completed()]
        env1 = sup.handle("worker_start", self.start_args(ids["request_id"]),
                          principal=PRINCIPAL)
        env2 = sup.handle("worker_start", self.start_args(ids["request_id"]),
                          principal=PRINCIPAL)
        self.assertTrue(env1["receipt"]["replayed"])
        self.assertTrue(env2["receipt"]["replayed"])
        self.assertEqual(env1["data"]["job"]["job_id"], ids["job_id"])
        self.assertEqual(fake.started, [])
        self.assertEqual(self.request_row(sup, PRINCIPAL,
                                          ids["request_id"])["phase"],
                         "prepared")
        sup.close()
        # the recovery supervisor dispatches exactly once — replays before
        # it cost nothing, and it cannot double with them
        sup2, fake2 = self.make_sup(
            steps=[("sleep", 0.6), outcome_completed()])
        self.wait_state(sup2, ids["job_id"], ("completed",))
        self.assertEqual(len(fake2.started), 1,
                         "duplicate suppression: exactly one dispatch ever")
        self.assertEqual(fake.started, [])
        self.assertEqual(len(self.attempts(sup2, ids["job_id"])), 1)
        self.assertEqual(self.request_row(sup2, PRINCIPAL,
                                          ids["request_id"])["phase"],
                         "completed")

    def test_ambiguous_replay_never_dispatches(self):
        ids = self.crash_to("sending")
        sup, fake = self.make_sup(
            steps=[outcome_completed()])  # ready to start IF it ever dared
        env = sup.handle("worker_start", self.start_args(ids["request_id"]),
                         principal=PRINCIPAL)
        self.assertTrue(env["receipt"]["replayed"])
        self.assertEqual(env["data"]["job"]["job_id"], ids["job_id"])
        self.assertEqual(self.job_row(sup, ids["job_id"])["state"],
                         "needs_attention")
        self.assertEqual(self.request_row(sup, PRINCIPAL,
                                          ids["request_id"])["phase"],
                         "ambiguous")
        self.assertFalse(
            sup.journal.claim_dispatch(PRINCIPAL, ids["request_id"]))
        self.assertEqual(fake.started, [],
                         "replaying an ambiguous request dispatches nothing")

    def test_followup_resume_dispatches_exactly_once(self):
        ids = self.crash_to("followup-prepared")
        sup, fake = self.make_sup(steps=[outcome_completed()])
        self.wait_state(sup, ids["job_id"], ("completed",))
        self.assertEqual(len(fake.started), 1,
                         "the journaled followup resumes with one dispatch")
        atts = self.attempts(sup, ids["job_id"])
        self.assertEqual(len(atts), 2, "no duplicate attempt from resume")
        fu_req = self.request_row(sup, PRINCIPAL, ids["followup_request_id"])
        self.assertEqual(fu_req["phase"], "completed")
        # A post-resume replay attempt must never add attempts or prompts.
        env = sup.handle("worker_followup", {
            "request_id": ids["followup_request_id"],
            "user_request_ref": "req/2026/crash-fu-0001",
            "job_id": ids["job_id"],
            "expected_attempt_id": ids["attempt_id"],
            "mode": "continue", "message": "synthetic followup"},
            principal=PRINCIPAL)
        self.assertIn("receipt", env)
        self.assertEqual(len(self.attempts(sup, ids["job_id"])), 2)
        self.assertEqual(len(fake.started), 1)
        self.assertEqual(self.request_row(sup, PRINCIPAL,
                                          ids["followup_request_id"])
                         ["phase"], "completed")
