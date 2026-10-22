"""U-M1..U-M3 — M3 N1 per-task model selection (D2/D6).

U-M1 approved-only execution: nothing dispatches without a user
  approval record for that task; the approval's exact model is the
  only dispatchable model and the reported effective model is
  verified against it.
U-M2 unapproved switches blocked: free→free and any Go switch is a
  HARD refusal without a fresh user decision; an unavailable model
  parks in waiting_model_selection and NEVER falls back.
U-M3 selection recorded: the approval record carries the user-
  decision evidence, history is append-only, billing_class stays
  honest (unknown is never recorded free), and records persist.

The real supervisor.ModelCatalog is used throughout: the policy is
duck-typed against it, exactly as a future wiring would be.
"""
from __future__ import annotations

import datetime
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hina_opencode_worker import models
from hina_opencode_worker.models import ModelPolicyError, ModelSelectionPolicy
from hina_opencode_worker.supervisor import ModelCatalog

CLOCK = 1764000000.0
DECIDED_AT = datetime.datetime.fromtimestamp(
    CLOCK, datetime.timezone.utc).isoformat(timespec="seconds")


def catalog() -> ModelCatalog:
    c = ModelCatalog()
    c.add("fake/free", "free", probe_status="live_verified")
    c.add("fake/free2", "free", probe_status="live_verified")
    c.add("fake/go", "go", probe_status="live_verified")
    c.add("fake/off", "free", probe_status="unavailable")
    c.add("fake/dis", "free", enabled=False)
    c.add("fake/unc", "unknown", probe_status="live_verified")
    return c


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hina-um-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "model_approvals.sqlite"
        self.catalog = catalog()
        self.policy = self.new_policy()
        self.addCleanup(self.policy.close)

    def new_policy(self):
        p = ModelSelectionPolicy(self.catalog, db_path=self.db,
                                 clock=lambda: CLOCK)
        self.addCleanup(p.close)
        return p

    def select(self, task="t1", model="fake/free", ref="ui/1"):
        return self.policy.select(task, model, user_decision_ref=ref,
                                  decided_by="user:owner")

    def refusal(self, fn, *a, **kw):
        with self.assertRaises(ModelPolicyError) as cm:
            fn(*a, **kw)
        return cm.exception

    def assertParks(self, err, reason):
        """Refusal that must park: contract classification preserved,
        and the refusal is never a fallback offer."""
        self.assertEqual(err.details["disposition"],
                         models.WAITING_MODEL_SELECTION)
        self.assertEqual(err.details["reason_code"], reason)
        self.assertIs(err.details["auto_switch"], False)


class UM1ApprovedOnlyExecution(PolicyTestCase):
    """U-M1 — approved-only execution."""

    def test_no_approval_refuses_dispatch_and_parks(self):
        err = self.refusal(self.policy.plan_dispatch, "t1")
        self.assertEqual(err.code, "MODEL_NOT_ALLOWED")
        self.assertParks(err, "no_approved_model")

    def test_parking_classification_matches_states_contract(self):
        states = json.loads(
            (Path(models.__file__).parent / "states.json").read_text())
        self.assertEqual(
            models.WAITING_MODEL_SELECTION, "waiting_model_selection")
        self.assertIn(models.WAITING_MODEL_SELECTION, states["states"])

    def test_select_without_user_decision_ref_refused(self):
        for kw in ({"decided_by": "user:o"},
                   {"user_decision_ref": "   ", "decided_by": "user:o"},
                   {"user_decision_ref": None, "decided_by": "user:o"}):
            err = self.refusal(self.policy.select, "t1", "fake/free", **kw)
            self.assertEqual(err.code, "INVALID_ARGUMENT")
        err = self.refusal(self.policy.select, "t1", "fake/free",
                           user_decision_ref="ui/1")
        self.assertEqual(err.code, "INVALID_ARGUMENT")
        self.assertIsNone(self.policy.approval("t1"))

    def test_approved_model_dispatches_exactly_and_verifies(self):
        rec = self.select()
        plan = self.policy.plan_dispatch("t1")
        self.assertEqual(plan.model, "fake/free")
        self.assertEqual(plan.billing_class, "free")
        self.assertEqual(plan.record_id, rec.record_id)
        # the approved model itself is an acceptable requested_model
        self.assertEqual(
            self.policy.plan_dispatch("t1", requested_model="fake/free")
            .model, "fake/free")
        self.assertEqual(
            self.policy.verify_effective(plan, "fake/free"), "fake/free")

    def test_dispatch_with_other_requested_model_hard_refused(self):
        self.select()
        err = self.refusal(self.policy.plan_dispatch, "t1",
                           requested_model="fake/free2")
        self.assertEqual(err.code, "MODEL_NOT_ALLOWED")
        self.assertEqual(err.details["reason_code"], "unapproved_switch")
        self.assertIs(err.details["auto_switch"], False)
        # the refusal did not move the approval or add a record
        self.assertEqual(self.policy.approval("t1").model, "fake/free")
        self.assertEqual(len(self.policy.history("t1")), 1)

    def test_effective_model_mismatch_or_unreported_hard_refused(self):
        plan = self.policy.plan_dispatch(
            "t1") if self.select() else None
        for reported in ("fake/go", " fake/free2 ", "", None, "   "):
            err = self.refusal(self.policy.verify_effective, plan,
                               reported)
            self.assertEqual(err.code, "MODEL_NOT_ALLOWED")
            self.assertParks(
                err, "effective_model_unreported"
                if reported is None or not reported.strip()
                else "effective_model_mismatch")


class UM2UnapprovedSwitchBlocked(PolicyTestCase):
    """U-M2 — free→free and Go are blocked without a user decision."""

    def test_free_to_free_switch_without_decision_blocked(self):
        self.select(model="fake/free")
        err = self.refusal(self.policy.plan_dispatch, "t1",
                           requested_model="fake/free2")
        self.assertEqual(err.code, "MODEL_NOT_ALLOWED")
        self.assertIs(err.details["auto_switch"], False)

    def test_go_switch_without_decision_blocked(self):
        self.select(model="fake/free")
        err = self.refusal(self.policy.plan_dispatch, "t1",
                           requested_model="fake/go")
        self.assertEqual(err.code, "MODEL_NOT_ALLOWED")
        self.assertEqual(err.details["reason_code"], "unapproved_switch")
        self.assertEqual(self.policy.approval("t1").model, "fake/free")

    def test_approved_unavailable_parks_never_falls_back(self):
        self.select(model="fake/free")
        self.catalog.add("fake/free", "free", probe_status="unavailable")
        err = self.refusal(self.policy.plan_dispatch, "t1")
        self.assertEqual(err.code, "MODEL_UNAVAILABLE")
        self.assertParks(err, "model_unavailable")
        # never re-pointed at free2/go: still the user's model, one record
        self.assertEqual(self.policy.approval("t1").model, "fake/free")
        self.assertEqual(len(self.policy.history("t1")), 1)

    def test_catalog_withdrawal_after_approval_parks_at_dispatch(self):
        self.select(model="fake/free")
        self.catalog.add("fake/free", "free", enabled=False)
        err = self.refusal(self.policy.plan_dispatch, "t1")
        self.assertEqual(err.code, "MODEL_NOT_ALLOWED")
        self.assertParks(err, "approval_invalid_catalog")

    def test_explicit_user_free_to_free_switch_allowed(self):
        self.select(model="fake/free")
        self.select(model="fake/free2", ref="ui/explicit-free")
        plan = self.policy.plan_dispatch("t1")
        self.assertEqual(plan.model, "fake/free2")
        self.assertEqual(plan.billing_class, "free")

    def test_explicit_user_switch_to_go_allowed(self):
        # D2: the user may choose Go after the report — what is banned
        # is the worker switching there without that decision.
        self.select(model="fake/free")
        rec = self.select(model="fake/go", ref="ui/user-picked-go")
        plan = self.policy.plan_dispatch("t1")
        self.assertEqual(plan.model, "fake/go")
        self.assertEqual(plan.billing_class, "go")
        self.assertEqual(rec.user_decision_ref, "ui/user-picked-go")

    def test_select_unlisted_disabled_or_unavailable_refused_no_record(self):
        for model, code in (("vibe/yolo", "MODEL_NOT_ALLOWED"),
                            ("fake/dis", "MODEL_NOT_ALLOWED"),
                            ("fake/off", "MODEL_UNAVAILABLE")):
            err = self.refusal(self.policy.select, "t2", model,
                               user_decision_ref="ui/x",
                               decided_by="user:owner")
            self.assertEqual(err.code, code)
            self.assertIs(err.details["auto_switch"], False)
        self.assertIsNone(self.policy.approval("t2"))
        self.assertEqual(self.policy.records(), ())


class UM3SelectionRecorded(PolicyTestCase):
    """U-M3 — the selection and its user-decision evidence are recorded."""

    def test_selection_record_carries_approval_evidence(self):
        rec = self.select(task="t9", model="fake/free", ref="ui/decision-7")
        stored = self.policy.approval("t9")
        self.assertEqual(stored.as_dict(), rec.as_dict())
        self.assertEqual(stored.task_id, "t9")
        self.assertEqual(stored.model, "fake/free")
        self.assertEqual(stored.billing_class, "free")
        self.assertEqual(stored.user_decision_ref, "ui/decision-7")
        self.assertEqual(stored.decided_by, "user:owner")
        self.assertEqual(stored.decided_at, DECIDED_AT)
        self.assertTrue(rec.record_id.startswith("rec_"))

    def test_history_append_only_latest_wins(self):
        self.select(ref="ui/one")
        self.select(model="fake/go", ref="ui/two")
        hist = self.policy.history("t1")
        self.assertEqual([r.user_decision_ref for r in hist],
                         ["ui/one", "ui/two"])
        self.assertEqual([r.model for r in hist], ["fake/free", "fake/go"])
        self.assertEqual(self.policy.approval("t1").model, "fake/go")

    def test_unknown_billing_never_recorded_as_free(self):
        rec = self.select(model="fake/unc")
        self.assertEqual(rec.billing_class, "unknown")
        self.assertEqual(self.policy.approval("t1").billing_class,
                         "unknown")

    def test_records_survive_reopen(self):
        rec = self.select(task="t5", ref="ui/persist")
        self.policy.close()
        reopened = self.new_policy()
        stored = reopened.approval("t5")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.as_dict(), rec.as_dict())

    def test_table_is_append_only(self):
        self.select()
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE model_approvals SET model='fake/go'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM model_approvals")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(len(self.policy.records()), 1)


if __name__ == "__main__":
    unittest.main()
