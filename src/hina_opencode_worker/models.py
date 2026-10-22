"""M3 N1 — per-task model selection policy (D2/D6).

A task may only dispatch on a model the user explicitly approved FOR
THAT TASK. select() is the only selection means and requires a
user_decision_ref — the record of the user's decision after the
situation/partial-result report (D2). plan_dispatch() verifies the
approval at dispatch and verify_effective() verifies the effective
model the backend actually runs; any mismatch or unreported model is a
HARD refusal, never a silent continuation.

There is no automatic switch, anywhere: not free→free, not to Go, not
on retry, not as fallback. An unavailable or never-approved model
refuses dispatch with details["disposition"] == WAITING_MODEL_SELECTION
(the contract classification in states.json 0.3, preserved) so the
caller parks the task and awaits a user decision. A Go (or any) switch
is possible only through select() with a fresh user decision.

billing_class is never inferred: unknown stays unknown and is recorded
as such — never rendered free. Job-state gating (JOB_BUSY etc.) stays
in the supervisor; this module owns approval authority and dispatch
verification only. Stdlib-only; the sqlite connection is thread-bound
(one thread owns a policy instance).
"""
from __future__ import annotations

import datetime
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

__all__ = ["WAITING_MODEL_SELECTION", "ModelPolicyError", "ApprovalRecord",
           "DispatchPlan", "ModelSelectionPolicy"]

#: states.json 0.3 classification — refused dispatch parks here for the
#: user's decision; it is never bypassed by picking another model.
WAITING_MODEL_SELECTION = "waiting_model_selection"

_MODEL_RE = re.compile(r"^[^\s/]+/[^\s]+$")  # worker_start contract shape
_MAX_TEXT = 512
_BILLING = ("free", "go", "unknown")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_approvals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_class TEXT NOT NULL,
    user_decision_ref TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS model_approvals_task_idx
    ON model_approvals (task_id, seq);
CREATE TRIGGER IF NOT EXISTS model_approvals_no_update
BEFORE UPDATE ON model_approvals
BEGIN SELECT RAISE(ABORT, 'model_approvals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS model_approvals_no_delete
BEFORE DELETE ON model_approvals
BEGIN SELECT RAISE(ABORT, 'model_approvals is append-only'); END;
"""


class ModelPolicyError(Exception):
    """Contract-shaped refusal. details["disposition"] tells the caller
    what to apply: WAITING_MODEL_SELECTION means park the task, close
    the attempt failed and await the user's decision — NEVER pick a
    different model. details["auto_switch"] is always False; it exists
    so no caller can mistake a refusal for a fallback offer."""

    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        # retry means the user decides again, not that the worker may
        # proceed on its own.
        return self.code == "MODEL_UNAVAILABLE"

    def to_error(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": self.details}


@dataclass(frozen=True)
class ApprovalRecord:
    """One user decision, appended and immutable. billing_class is the
    honest catalog class at decision time — unknown stays unknown."""
    record_id: str
    task_id: str
    model: str
    billing_class: str
    user_decision_ref: str
    decided_by: str
    decided_at: str
    note: str
    seq: int

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id, "task_id": self.task_id,
            "model": self.model, "billing_class": self.billing_class,
            "user_decision_ref": self.user_decision_ref,
            "decided_by": self.decided_by, "decided_at": self.decided_at,
            "note": self.note, "seq": self.seq,
        }


@dataclass(frozen=True)
class DispatchPlan:
    """The only model a dispatch may use, plus the approval behind it."""
    task_id: str
    model: str
    billing_class: str
    record_id: str


def _require_text(value, field: str, *, pattern=None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelPolicyError("INVALID_ARGUMENT",
                               f"{field} must be a non-empty string")
    v = value.strip()
    if len(v) > _MAX_TEXT:
        raise ModelPolicyError("INVALID_ARGUMENT",
                               f"{field} exceeds {_MAX_TEXT} characters")
    if pattern is not None and not pattern.match(v):
        raise ModelPolicyError("INVALID_ARGUMENT",
                               f"{field} does not match the contract shape")
    return v


def _honest_billing(row) -> str:
    if not row:
        return "unknown"
    bc = row.get("billing_class")
    return bc if bc in _BILLING else "unknown"


class ModelSelectionPolicy:
    """Per-task model approval authority. catalog is duck-typed against
    supervisor.ModelCatalog (.get(model_id) -> row dict or None)."""

    def __init__(self, catalog, *, db_path=None, clock=time.time):
        self._catalog = catalog
        self._clock = clock
        self._db = sqlite3.connect(str(db_path) if db_path is not None
                                   else ":memory:")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- selection: the ONLY means of choosing or switching a model ---

    def select(self, task_id, model, *, user_decision_ref=None,
               decided_by=None, note="") -> ApprovalRecord:
        """Record the user's model decision for this task and make it
        the active approval. A switch away from another model (any
        class, including free→free and any Go target) is legal ONLY
        through this call with a fresh user_decision_ref."""
        task = _require_text(task_id, "task_id")
        model = _require_text(model, "model", pattern=_MODEL_RE)
        ref = _require_text(user_decision_ref, "user_decision_ref")
        who = _require_text(decided_by, "decided_by")
        if not isinstance(note, str):
            raise ModelPolicyError("INVALID_ARGUMENT",
                                   "note must be a string")
        note = note.strip()
        if len(note) > 2000:
            raise ModelPolicyError("INVALID_ARGUMENT",
                                   "note exceeds 2000 characters")
        self._gate(model, "refused", "selection")
        row = self._catalog.get(model)
        rec = ApprovalRecord(
            record_id="rec_" + str(uuid.uuid4()),
            task_id=task, model=model,
            billing_class=_honest_billing(row),
            user_decision_ref=ref, decided_by=who,
            decided_at=datetime.datetime.fromtimestamp(
                self._clock(), datetime.timezone.utc
            ).isoformat(timespec="seconds"),
            note=note, seq=0)
        cur = self._db.execute(
            "INSERT INTO model_approvals (record_id, task_id, model,"
            " billing_class, user_decision_ref, decided_by, decided_at,"
            " note) VALUES (?,?,?,?,?,?,?,?)",
            (rec.record_id, rec.task_id, rec.model, rec.billing_class,
             rec.user_decision_ref, rec.decided_by, rec.decided_at, note))
        self._db.commit()
        return ApprovalRecord(**{**rec.as_dict(), "seq": cur.lastrowid})

    # -- records -------------------------------------------------------

    def approval(self, task_id) -> ApprovalRecord | None:
        """The active approval: the user's latest recorded decision."""
        task = _require_text(task_id, "task_id")
        row = self._db.execute(
            "SELECT * FROM model_approvals WHERE task_id = ?"
            " ORDER BY seq DESC LIMIT 1", (task,)).fetchone()
        return self._to_record(row)

    def history(self, task_id) -> tuple:
        """Append-only audit trail for the task, oldest first."""
        task = _require_text(task_id, "task_id")
        rows = self._db.execute(
            "SELECT * FROM model_approvals WHERE task_id = ?"
            " ORDER BY seq", (task,)).fetchall()
        return tuple(self._to_record(r) for r in rows if r is not None)

    def records(self) -> tuple:
        rows = self._db.execute(
            "SELECT * FROM model_approvals ORDER BY seq").fetchall()
        return tuple(self._to_record(r) for r in rows if r is not None)

    # -- dispatch-time verification -------------------------------------

    def plan_dispatch(self, task_id, *, requested_model=None) -> DispatchPlan:
        """Gate a dispatch against the approval. Refusals never fall
        back to another model: no approval, or an approval the catalog
        no longer backs/parks, refuses with disposition
        WAITING_MODEL_SELECTION; a requested_model different from the
        approved one is a HARD refusal (a switch means select())."""
        task = _require_text(task_id, "task_id")
        if requested_model is not None:
            requested_model = _require_text(requested_model,
                                            "requested_model",
                                            pattern=_MODEL_RE)
        rec = self.approval(task)
        if rec is None:
            raise ModelPolicyError(
                "MODEL_NOT_ALLOWED",
                f"task {task!r} has no user-approved model; dispatch is"
                " refused until the user selects one (D2)",
                details={"disposition": WAITING_MODEL_SELECTION,
                         "reason_code": "no_approved_model",
                         "auto_switch": False})
        if requested_model is not None and requested_model != rec.model:
            raise ModelPolicyError(
                "MODEL_NOT_ALLOWED",
                f"dispatch of task {task!r} requested {requested_model!r}"
                f" but {rec.model!r} is the approved model; unapproved"
                " switches are refused — a switch requires select() with"
                " a fresh user_decision_ref",
                details={"disposition": "refused",
                         "reason_code": "unapproved_switch",
                         "approved_model": rec.model,
                         "requested_model": requested_model,
                         "auto_switch": False})
        self._gate(rec.model, WAITING_MODEL_SELECTION, "dispatch")
        return DispatchPlan(task_id=task, model=rec.model,
                            billing_class=rec.billing_class,
                            record_id=rec.record_id)

    def verify_effective(self, plan: DispatchPlan,
                         reported_model) -> str:
        """Verify the effective model the backend actually runs against
        the approved plan. Mismatch or an unreported model is a HARD
        refusal — the run must not continue on an unapproved model;
        the caller closes the attempt and parks the task."""
        if reported_model is None or not str(reported_model).strip():
            raise ModelPolicyError(
                "MODEL_NOT_ALLOWED",
                "effective model was not reported; an unverifiable model"
                " is never treated as approved",
                details={"disposition": WAITING_MODEL_SELECTION,
                         "reason_code": "effective_model_unreported",
                         "approved_model": plan.model,
                         "auto_switch": False})
        reported = str(reported_model).strip()
        if reported != plan.model:
            raise ModelPolicyError(
                "MODEL_NOT_ALLOWED",
                f"effective model {reported!r} is not the approved model"
                f" {plan.model!r}; the run must not continue on it",
                details={"disposition": WAITING_MODEL_SELECTION,
                         "reason_code": "effective_model_mismatch",
                         "approved_model": plan.model,
                         "effective_model": reported,
                         "auto_switch": False})
        return plan.model

    # -- internals -------------------------------------------------------

    def _gate(self, model: str, disposition: str, phase: str) -> None:
        row = self._catalog.get(model)
        if row is None or not row.get("enabled", False):
            raise ModelPolicyError(
                "MODEL_NOT_ALLOWED",
                f"model {model!r} is not an approved, enabled catalog id",
                details={"disposition": disposition,
                         "reason_code":
                             "model_not_approved" if phase == "selection"
                             else "approval_invalid_catalog",
                         "billing_class": _honest_billing(row),
                         "auto_switch": False})
        if row.get("probe_status") == "unavailable":
            raise ModelPolicyError(
                "MODEL_UNAVAILABLE",
                f"model {model!r} is currently marked unavailable; no"
                " fallback model will be chosen (D2)",
                details={"disposition": disposition,
                         "reason_code": "model_unavailable",
                         "billing_class": _honest_billing(row),
                         "auto_switch": False})

    def _to_record(self, row) -> ApprovalRecord | None:
        if row is None:
            return None
        return ApprovalRecord(
            record_id=row["record_id"], task_id=row["task_id"],
            model=row["model"], billing_class=row["billing_class"],
            user_decision_ref=row["user_decision_ref"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"], note=row["note"],
            seq=row["seq"])
