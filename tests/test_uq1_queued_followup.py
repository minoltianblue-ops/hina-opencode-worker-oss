"""UQ-1..UQ-3 — M2-Q2 queued followup (supervisor, coordinator-frozen
shape): worker_followup mode=continue + optional delivery ∈ [steer,
queue] (observed DELIVERY_ENUM), admitted ONLY while the job runs.

UQ-1 steer: accepted immediately against the running attempt's live
       session — one injection, job stays running, no new attempt, no
       result touched, replay never re-injects (isolation).
UQ-2 queue: deferred and EXPLICIT-only — busy without delivery stays
       JOB_BUSY (M1 verbatim, no auto-queue); with delivery the request
       is accepted and handed to the backend as a deferred independent
       turn the supervisor never auto-starts or adopts, and delivery
       off a running job (or off shape) is refused.
UQ-3 budgets (MCP.md §4): a slow delivery times out inside
       CONTROL_TIMEOUT_S — never extended, no auto re-send; the request
       stays journaled 'accepted' (ambiguous by design), the job is
       untouched, and a concurrent worker_wait still returns inside its
       own wait_ms budget.
"""
from __future__ import annotations

import time
import types
import unittest

from _p4_support import (FakeBackend, P4TestCase, assert_envelope,
                         fake_att_id, outcome_completed, rid)
from hina_opencode_worker import supervisor as sup_mod


class DeliveryFake(FakeBackend):
    """FakeBackend + recording prompt seam (observed prompt API)."""

    def __init__(self):
        super().__init__()
        self.prompts: list[tuple] = []
        self.prompt_sleep = 0.0
        self.prompt_error: Exception | None = None
        self.prompt_ok = True

    def prompt(self, session_id, text, delivery):
        self.prompts.append((session_id, text, delivery))
        if self.prompt_error is not None:
            raise self.prompt_error
        if self.prompt_sleep:
            time.sleep(self.prompt_sleep)
        return types.SimpleNamespace(ok=self.prompt_ok)


class QueuedFollowupBase(P4TestCase):
    def setUp(self):
        super().setUp()
        b = DeliveryFake()
        self.sup.backend = b
        self.backend = b
        self._jobs: list[tuple] = []

    def tearDown(self):
        # drain every live runner BEFORE the temp root disappears: a
        # cancelled run makes the fake wait slices exit immediately, so
        # no thread touches the DB after teardown (clean logs, no
        # crash-window false positives)
        for jid, att in self._jobs:
            try:
                self.call("worker_cancel", {"request_id": rid(),
                                            "job_id": jid,
                                            "expected_attempt_id": att,
                                            "reason": "test teardown"})
            except Exception:
                pass
        end = time.monotonic() + 5.0
        while time.monotonic() < end and any(
                t.is_alive() for t in list(self.sup._runners.values())):
            time.sleep(0.05)
        super().tearDown()

    def _running_job(self):
        self.backend.steps = [("hang", None)] * 1000 + [outcome_completed()]
        env = self.call("worker_start", self.start_args(rid()))
        jid = env["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        att = self.sup._job_row(jid)["active_attempt_id"]
        sid = self.backend.started[-1]["session_id"]
        self._jobs.append((jid, att))
        return jid, att, sid

    def _followup(self, jid, att, rid_, *, mode="continue", **over):
        a = {"request_id": rid_, "user_request_ref": "chat/1",
             "job_id": jid, "expected_attempt_id": att,
             "mode": mode, "message": "prefer stdlib only"}
        a.update(over)
        return self.call("worker_followup", a)

    def _attempt_count(self, jid) -> int:
        conn = self.sup._read()
        try:
            return conn.execute(
                "SELECT COUNT(*) c FROM attempts WHERE job_id = ?",
                (jid,)).fetchone()["c"]
        finally:
            conn.close()


class UQ1SteerImmediate(QueuedFollowupBase):
    def test_uq1_steer_injects_once_into_active_session(self):
        jid, att, sid = self._running_job()
        before = self.call("worker_status", {"job_id": jid})[
            "data"]["jobs"][0]
        rid_ = rid()
        env = self._followup(jid, att, rid_, delivery="steer")
        assert_envelope(self, env, "worker_followup")
        self.assertFalse(env["receipt"]["replayed"])
        # immediate: exactly one injection, into the ACTIVE attempt's
        # session only — never another job's/attempt's session
        self.assertEqual(self.backend.prompts,
                         [(sid, "prefer stdlib only", "steer")])
        # isolation: running attempt keeps sole ownership; no new
        # attempt, no job transition, result untouched
        self.assertEqual(self.job_state(jid), "running")
        self.assertEqual(self.sup._job_row(jid)["active_attempt_id"], att)
        self.assertEqual(self._attempt_count(jid), 1)
        after = self.call("worker_status", {"job_id": jid})["data"]["jobs"][0]
        self.assertEqual(after["result_revision"], before["result_revision"])
        # replay with the SAME request_id: accepted once, never re-injected
        env2 = self._followup(jid, att, rid_, delivery="steer")
        self.assertTrue(env2["receipt"]["replayed"], str(env2)[:200])
        self.assertEqual(len(self.backend.prompts), 1)
        # a stale expected_attempt_id cannot steer even with delivery
        env3 = self._followup(jid, fake_att_id(), rid(), delivery="steer")
        self.assertEqual(env3["error"]["code"], "STALE_ATTEMPT")


class UQ2QueueDeferredExplicitOnly(QueuedFollowupBase):
    def test_uq2_queue_deferred_no_auto_next_job(self):
        jid, att, sid = self._running_job()
        # M1 verbatim: busy followup WITHOUT delivery is JOB_BUSY and
        # nothing is queued internally (no auto-queue).
        env = self._followup(jid, att, rid())
        self.assertEqual(env["error"]["code"], "JOB_BUSY")
        self.assertEqual(self.backend.prompts, [])
        # explicit queue: accepted while busy; the backend owns the
        # deferred independent turn (delivery=queue)
        rid_q = rid()
        env = self._followup(jid, att, rid_q, delivery="queue")
        assert_envelope(self, env, "worker_followup")
        self.assertEqual(self.backend.prompts,
                         [(sid, "prefer stdlib only", "queue")])
        self.assertEqual(self.job_state(jid), "running")
        req = self.sup._request_row(self.uid_principal(), rid_q)
        self.assertIsNotNone(req)          # durable journal, not memory
        self.assertEqual(req["phase"], "completed")
        # no auto-start, ever: the deferred turn is NOT adopted as a
        # supervisor attempt and starts no next job on completion
        self.backend.runs[sid] = [outcome_completed()]
        self.wait_for(jid, ("completed",))
        self.assertEqual(len(self.backend.started), 1)   # one start total
        self.assertEqual(self._attempt_count(jid), 1)    # never absorbed
        self.assertEqual(self.sup._job_row(jid)["active_attempt_id"], att)
        # off a running job delivery is refused outright (rule 4)
        env = self._followup(jid, att, rid(), delivery="steer")
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")

    def test_uq2_delivery_shape_rules(self):
        jid, att, _ = self._running_job()
        # delivery rides mode=continue only
        env = self._followup(jid, att, rid(), mode="retry",
                             delivery="steer")
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")
        env = self._followup(jid, att, rid(), mode="select_model",
                             delivery="queue", model="fake/free",
                             user_decision_ref="ui/1")
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")
        # delivery enum is the observed pair, nothing else
        env = self._followup(jid, att, rid(), delivery="park")
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")
        # unknown extra fields still rejected (M1 strictness kept)
        env = self._followup(jid, att, rid(), delivery="steer",
                             queue=True)
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.backend.prompts, [])


class UQ3WaitBudgets(QueuedFollowupBase):
    def test_uq3_delivery_budget_never_extended(self):
        jid, att, sid = self._running_job()
        self.backend.prompt_sleep = sup_mod.CONTROL_TIMEOUT_S + 1.5
        rid_ = rid()
        t0 = time.monotonic()
        env = self._followup(jid, att, rid_, delivery="steer")
        spent = time.monotonic() - t0
        self.assertEqual(env["error"]["code"], "BACKEND_TIMEOUT")
        self.assertLess(spent, sup_mod.CONTROL_TIMEOUT_S + 1.0,
                        "delivery must never extend the control budget")
        # ambiguous by design: journaled 'accepted', never silently
        # dropped, never auto-re-sent
        req = self.sup._request_row(self.uid_principal(), rid_)
        self.assertIsNotNone(req)
        self.assertEqual(req["phase"], "accepted")
        self.assertEqual(self.job_state(jid), "running")
        self.assertEqual(len(self.backend.prompts), 1)
        # same request_id replays the acceptance window — it never
        # triggers a second injection
        env2 = self._followup(jid, att, rid_, delivery="steer")
        self.assertTrue(env2["receipt"]["replayed"], str(env2)[:200])
        self.assertEqual(len(self.backend.prompts), 1)
        # a slow control path must not stretch worker_wait either (§4)
        self.backend.slow_reconcile_s = 15.0
        t1 = time.monotonic()
        env3 = self.call("worker_wait", {"job_id": jid, "wait_ms": 700})
        spent3 = time.monotonic() - t1
        self.assertTrue(env3["data"]["wait_expired"])
        self.assertLess(spent3, 3.0,
                        "worker_wait must return inside its own budget")

    def test_uq3_backend_refusal_is_not_a_silent_accept(self):
        jid, att, sid = self._running_job()
        self.backend.prompt_ok = False
        env = self._followup(jid, att, rid(), delivery="queue")
        self.assertEqual(env["error"]["code"], "BACKEND_UNAVAILABLE")
        self.assertEqual(self.job_state(jid), "running")
        self.assertEqual(self._attempt_count(jid), 1)


if __name__ == "__main__":
    unittest.main()
