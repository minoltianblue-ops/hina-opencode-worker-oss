"""T17 — model classification and selection policy (D2/D6).

- billing_class is never inferred; unknown stays unknown and never
  renders as free/zero-cost.
- an unavailable model parks in waiting_model_selection with the attempt
  closed failed — NEVER an automatic switch, not free→free, not to Go.
- select_model is the only switch means and needs an explicit
  user_decision_ref; it works only in waiting_model_selection.
- retries keep the user's model; nothing ever migrates to Go on its own.
"""
from __future__ import annotations

import unittest

from _p4_support import (
    P4TestCase, assert_envelope, outcome_completed, outcome_failed, rid)
from hina_opencode_worker.supervisor import ModelCatalog


class CatalogHonesty(P4TestCase):
    def test_unknown_billing_never_becomes_free(self):
        cat = self.call("worker_catalog", {"refresh": False})
        assert_envelope(self, cat, "worker_catalog")
        rows = {m["id"]: m for m in cat["data"]["models"]}
        self.assertEqual(rows["fake/unknown"]["billing_class"], "unknown")
        self.assertEqual(rows["fake/free"]["billing_class"], "free")
        self.assertEqual(rows["fake/go"]["billing_class"], "go")
        self.assertEqual(rows["fake/off"]["probe_status"], "unavailable")
        self.assertFalse(rows["fake/dis"]["enabled"])
        # an unknown catalog id also classifies unknown, never free
        self.assertEqual(self.catalog.billing_class("nope/x"), "unknown")

    def test_catalog_row_for_unclassified_model_defaults_unknown(self):
        c = ModelCatalog()
        self.assertEqual(c.billing_class("brand/new"), "unknown")
        self.assertEqual(c.rows(), [])


class UnknownBillingAttempt(P4TestCase):
    def test_attempt_persists_unknown_billing_not_free(self):
        jid = self.completed_start(rid(), model="fake/unknown")
        att = self.sup._latest_attempt_read(jid)
        self.assertEqual(att["model"], "fake/unknown")
        self.assertEqual(att["billing_class"], "unknown",
                         "unknown must never persist as free")
        res = self.call("worker_result", {"job_id": jid})
        self.assertEqual(res["data"]["job"]["state"], "completed")


class NoAutoSwitch(P4TestCase):
    def _park_selection(self, model="fake/free"):
        self.backend.unavailable.add(model)
        env = self.call("worker_start", self.start_args(rid(),
                                                        model=model))
        jid = env["data"]["job"]["job_id"]
        self.wait_for(jid, ("waiting_model_selection",))
        return jid

    def test_unavailable_parks_selection_and_closes_attempt_failed(self):
        jid = self._park_selection()
        job = self.sup._job_row(jid)
        self.assertEqual(job["state"], "waiting_model_selection")
        conn = self.sup._read()
        try:
            ev = conn.execute(
                "SELECT reason_code FROM state_events WHERE job_id = ?"
                " AND to_state = 'waiting_model_selection'",
                (jid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(ev["reason_code"], "model_unavailable")
        att = self.sup._latest_attempt_read(jid)
        self.assertEqual(att["state"], "failed",
                         "the attempt is closed failed, never re-pointed")
        self.assertEqual(att["model"], "fake/free")
        # no backend session ever started with another model
        started_models = {s["model"] for s in self.backend.started}
        self.assertTrue(started_models <= {"fake/free"},
                        f"backend started {started_models} without a"
                        " user decision")
        # the job is NOT auto-queued onto the next model either
        self.assertEqual(self.job_state(jid), "waiting_model_selection")

    def test_select_model_only_with_user_decision(self):
        jid = self._park_selection()
        att = self.sup._job_row(jid)["active_attempt_id"]
        # no user_decision_ref -> refused
        env = self.call("worker_followup",
                        {"request_id": rid(), "user_request_ref": "r/s",
                         "job_id": jid, "expected_attempt_id": att,
                         "mode": "select_model",
                         "message": "use the other one"})
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")
        # not in the catalog -> MODEL_NOT_ALLOWED
        env = self.call("worker_followup",
                        {"request_id": rid(), "user_request_ref": "r/s2",
                         "job_id": jid, "expected_attempt_id": att,
                         "mode": "select_model", "model": "vibe/yolo",
                         "message": "please",
                         "user_decision_ref": "ui/1"})
        self.assertEqual(env["error"]["code"], "MODEL_NOT_ALLOWED")
        # explicitly disabled model -> MODEL_NOT_ALLOWED
        env = self.call("worker_followup",
                        {"request_id": rid(), "user_request_ref": "r/s3",
                         "job_id": jid, "expected_attempt_id": att,
                         "mode": "select_model", "model": "fake/dis",
                         "message": "please",
                         "user_decision_ref": "ui/1"})
        self.assertEqual(env["error"]["code"], "MODEL_NOT_ALLOWED")
        # explicit free→free switch ONLY as an explicit user decision
        self.catalog.add("fake/free2", "free", probe_status="live_verified")
        self.backend.steps = [outcome_completed()]
        env = self.call("worker_followup",
                        {"request_id": rid(), "user_request_ref": "r/s4",
                         "job_id": jid, "expected_attempt_id": att,
                         "mode": "select_model", "model": "fake/free2",
                         "message": "same class, user picked it",
                         "user_decision_ref": "ui/explicit-free"})
        assert_envelope(self, env, "worker_followup")
        self.assertIn("data", env, str(env)[:200])
        self.wait_for(jid, ("completed", "failed"))
        att2 = self.sup._latest_attempt_read(jid)
        self.assertEqual(att2["model"], "fake/free2")
        self.assertEqual(att2["ordinal"], 2)

    def test_no_silent_migration_to_go_on_retry(self):
        # Go job fails once; the user retries. retry keeps the model —
        # and a free request must never be migrated to Go either (mirror
        # asserted in test_unknown_billing_never_becomes_free).
        self.backend.steps = [outcome_failed()]
        env = self.call("worker_start", self.start_args(rid(),
                                                        model="fake/go"))
        jid = env["data"]["job"]["job_id"]
        self.wait_for(jid, ("failed",))
        att = self.sup._job_row(jid)["active_attempt_id"]
        self.backend.steps = [outcome_completed()]
        env = self.call("worker_followup",
                        {"request_id": rid(), "user_request_ref": "r/r",
                         "job_id": jid, "expected_attempt_id": att,
                         "mode": "retry",
                         "message": "run it again"})
        assert_envelope(self, env, "worker_followup")
        self.assertIn("data", env, str(env)[:200])
        self.wait_for(jid, ("completed", "failed"))
        conn = self.sup._read()
        try:
            rows = conn.execute(
                "SELECT model, billing_class FROM attempts WHERE job_id"
                " = ? ORDER BY ordinal", (jid,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["model"], "fake/go",
                             "retry must never migrate the model")
            self.assertEqual(r["billing_class"], "go")

    def test_select_model_refused_in_other_states(self):
        # running: JOB_BUSY (covered in T13) — here: waiting_approval
        saved = self.config.approval_channel_verified
        self.config.approval_channel_verified = True
        try:
            self.backend.steps = [
                ("permission", {"id": "perm_17",
                                "sessionID": "ses_fake0001",
                                "action": "shell",
                                "resources": ["ls"], "save": []}),
                outcome_completed()]
            jid = self.call("worker_start",
                            self.start_args(rid()))["data"]["job"]["job_id"]
            self.wait_for(jid, ("waiting_approval",))
            att = self.sup._job_row(jid)["active_attempt_id"]
            env = self.call("worker_followup",
                            {"request_id": rid(),
                             "user_request_ref": "r/wa",
                             "job_id": jid, "expected_attempt_id": att,
                             "mode": "select_model",
                             "model": "fake/go",
                             "message": "switch instead of approving",
                             "user_decision_ref": "ui/wa"})
            self.assertEqual(env["error"]["code"], "JOB_BUSY")
            self.assertIn("not a model-switch means",
                          env["error"]["message"])
            self.finish(jid)
        finally:
            self.config.approval_channel_verified = saved


if __name__ == "__main__":
    unittest.main()
