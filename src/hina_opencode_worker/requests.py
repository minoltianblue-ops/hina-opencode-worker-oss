"""Request idempotency per docs/contracts/STORAGE.md §3.

- PK (principal_id, request_id). tool + input are normalized to canonical
  JSON (UTF-8, key sort, compact separators, finite numbers only; no
  Unicode normalization or trimming) and stored as SHA-256 + canonical text.
- Existing request is looked up FIRST inside the transaction: same tool +
  same payload returns the stored receipt (replayed=True); different
  payload or different tool is a conflict. A replay is never blocked by
  the busy pre-check.
- Resolved defaults (model/profile/repository path) are frozen at first
  acceptance; replay never reapplies current defaults.
- Dispatch is claimed exactly once via a CAS phase flip
  prepared -> sending. sending/acknowledged with a lost response become
  'ambiguous' through Store.reconcile_after_restart and must never be
  re-dispatched. A request being 'recorded' is never claimed as 'executed'.
"""
from __future__ import annotations

import hashlib
import json

from .store import (
    ACTIVE_ATTEMPT_STATES,
    DatabaseLockedError,
    NotFoundError,
    Store,
    StoreError,
    new_id,
    utc_now,
)
from .state import COMPLETED, FAILED, STARTING

TOOLS = ("worker_start", "worker_followup", "worker_cancel", "worker_approve")

# requests.phase machine (schema CHECK is the outer bound; this is the order).
PHASE_GRAPH: dict[str, set[str]] = {
    "accepted": {"prepared", "failed"},
    "prepared": {"sending", "failed", "ambiguous"},
    "sending": {"acknowledged", "ambiguous"},
    "acknowledged": {"completed", "failed", "ambiguous"},
    "ambiguous": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}


class InvalidRequestError(StoreError):
    code = "INVALID_ARGUMENT"


class IdempotencyConflictError(StoreError):
    code = "IDEMPOTENCY_CONFLICT"


class BusyError(StoreError):
    code = "BUSY"


class PhaseConflictError(StoreError):
    code = "PHASE_CONFLICT"


def canonical_json(value) -> str:
    """Canonical form: key sort, compact separators, UTF-8, finite numbers
    only. No Unicode normalization, no trimming (STORAGE.md §3)."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except ValueError as e:  # NaN/Infinity
        raise InvalidRequestError(f"payload must use finite numbers only: {e}") from e


def payload_sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RequestJournal:
    """Idempotent intake + dispatch-claim on top of Store."""

    def __init__(self, store: Store, defaults: dict | None = None) -> None:
        self.store = store
        # Mutable defaults provider; frozen into the row at first acceptance.
        self.defaults = defaults if defaults is not None else {}

    # ------------------------------------------------------------- intake

    def _lookup(self, conn, principal_id: str, request_id: str):
        return conn.execute(
            "SELECT * FROM requests WHERE principal_id = ? AND request_id = ?",
            (principal_id, request_id),
        ).fetchone()

    def _receipt(self, row) -> dict:
        return json.loads(row["receipt_json"]) if row["receipt_json"] else {
            "request_id": row["request_id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "phase": row["phase"],
        }

    def _check_conflict(self, existing, tool: str, sha: str) -> None:
        if existing["tool"] != tool or existing["payload_sha256"] != sha:
            raise IdempotencyConflictError(
                f"request {existing['request_id']} already accepted with a"
                f" different {'tool' if existing['tool'] != tool else 'payload'}"
            )

    def intake(
        self,
        principal_id: str,
        request_id: str,
        tool: str,
        payload: dict,
        *,
        pre_accept_check=None,
        executor_id: str = "local",
        billing_class: str = "unknown",
        contract_revision: str = "0.3",
        binary_sha256: str = "0" * 64,
    ) -> tuple[dict, bool]:
        """worker_start intake (STORAGE.md §3 ①②). Returns (receipt, replayed).

        pre_accept_check(principal_id, request_id) may raise BusyError; it
        runs ONLY for a new request, never for a replay.
        """
        if tool not in TOOLS:
            raise InvalidRequestError(f"unknown tool {tool!r}")
        if tool != "worker_start":
            raise InvalidRequestError(
                f"{tool} must go through its dedicated intake"
            )
        canonical = canonical_json(payload)
        sha = payload_sha256(canonical)
        now = utc_now()
        with self.store.tx() as conn:
            existing = self._lookup(conn, principal_id, request_id)
            if existing is not None:
                self._check_conflict(existing, tool, sha)
                return self._receipt(existing), True

            if pre_accept_check is not None:
                pre_accept_check(principal_id, request_id)

            d = self.defaults
            goal = payload.get("goal") or d.get("goal")
            model = payload.get("model") or d.get("model")
            profile = payload.get("profile") or d.get("profile", "inspect")
            repository = (
                payload.get("workspace", {}).get("repository")
                if isinstance(payload.get("workspace"), dict)
                else None
            ) or d.get("repository_path")
            if not (goal and model and repository):
                raise InvalidRequestError(
                    "goal/model/repository are required (payload or frozen defaults)"
                )
            # Frozen-at-first-acceptance resolved intent (never re-resolved).
            resolved = dict(payload)
            resolved.setdefault("model", model)
            resolved.setdefault("profile", profile)
            resolved.setdefault("workspace", {"repository": repository})
            resolved_canonical = canonical_json(resolved)

            job_id = new_id("job")
            attempt_id = new_id("att")
            # FK order: jobs/attempts first, then the request row that
            # references them (schema FKs are immediate except the deferred
            # jobs.active_attempt_id).
            self.store.insert_job(
                conn,
                job_id=job_id,
                principal_id=principal_id,
                goal=goal,
                requested_model=model,
                profile=profile,
                repository_path=repository,
                base_revision=payload.get("workspace", {}).get("source_ref")
                if isinstance(payload.get("workspace"), dict)
                else None,
                now=now,
            )
            self.store.insert_attempt(
                conn,
                attempt_id=attempt_id,
                job_id=job_id,
                ordinal=1,
                executor_id=executor_id,
                model=model,
                billing_class=billing_class,
                state="accepted",
                contract_revision=contract_revision,
                binary_sha256=binary_sha256,
            )
            # §7: active_attempt_id set inside the intake tx, never null after.
            self.store.set_active_attempt(conn, job_id, attempt_id, 0, now)
            self.store.insert_request(
                conn,
                principal_id=principal_id,
                request_id=request_id,
                tool=tool,
                payload_sha256=sha,
                normalized_json=resolved_canonical,
                job_id=job_id,
                attempt_id=attempt_id,
                phase="accepted",
                receipt={
                    "request_id": request_id,
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "phase": "accepted",
                    "payload_sha256": sha,
                    "created_at": now,
                },
                created_at=now,
                updated_at=now,
            )
        return self._receipt(self.store.get_request(principal_id, request_id)), False

    def followup_intake(
        self,
        principal_id: str,
        request_id: str,
        job_id: str,
        payload: dict,
        *,
        executor_id: str = "local",
        billing_class: str = "unknown",
        contract_revision: str = "0.3",
        binary_sha256: str = "0" * 64,
    ) -> tuple[dict, bool]:
        """worker_followup intake. Existing request is resolved BEFORE any
        busy evaluation, so a replay is never blocked by the busy pre-check;
        only a NEW followup on a busy job gets BUSY."""
        canonical = canonical_json(payload)
        sha = payload_sha256(canonical)
        now = utc_now()
        with self.store.tx() as conn:
            existing = self._lookup(conn, principal_id, request_id)
            if existing is not None:
                self._check_conflict(existing, "worker_followup", sha)
                return self._receipt(existing), True

            job = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFoundError(f"job {job_id} not found")
            active = conn.execute(
                "SELECT attempt_id FROM attempts WHERE job_id = ? "
                f"AND state IN {ACTIVE_ATTEMPT_STATES!r}",
                (job_id,),
            ).fetchone()
            if active is not None or job["state"] not in (COMPLETED, FAILED):
                raise BusyError(
                    f"job {job_id} is busy (state={job['state']});"
                    " M1 allows one active attempt and no auto-queue"
                )

            new_attempt_id = new_id("att")
            ordinal = conn.execute(
                "SELECT MAX(ordinal) FROM attempts WHERE job_id = ?", (job_id,)
            ).fetchone()[0] + 1
            model = payload.get("model") or job["requested_model"]
            # Attempt row first (state_events.attempt_id FK is immediate),
            # then the journaled job transition, then the active pointer.
            self.store.insert_attempt(
                conn,
                attempt_id=new_attempt_id,
                job_id=job_id,
                ordinal=ordinal,
                executor_id=executor_id,
                model=model,
                billing_class=billing_class,
                state=STARTING,
                contract_revision=contract_revision,
                binary_sha256=binary_sha256,
            )
            self.store._transition_job_conn(
                conn, job_id, job["version"], STARTING, "followup",
                attempt_id=new_attempt_id, explicit_followup=True, now=now,
            )
            self.store.set_active_attempt(conn, job_id, new_attempt_id, job["version"] + 1, now)
            self.store.insert_request(
                conn,
                principal_id=principal_id,
                request_id=request_id,
                tool="worker_followup",
                payload_sha256=sha,
                normalized_json=canonical_json(payload),
                job_id=job_id,
                attempt_id=new_attempt_id,
                phase="accepted",
                receipt={
                    "request_id": request_id,
                    "job_id": job_id,
                    "attempt_id": new_attempt_id,
                    "phase": "accepted",
                    "payload_sha256": sha,
                    "created_at": now,
                },
                created_at=now,
                updated_at=now,
            )
        return self._receipt(self.store.get_request(principal_id, request_id)), False

    # ------------------------------------------------------- phase machine

    def _set_phase(self, principal_id: str, request_id: str, to_phase: str,
                   *, from_phase: str | None = None) -> bool:
        row = self.store.get_request(principal_id, request_id)
        if row is None:
            raise NotFoundError(f"request {request_id} not found")
        frm = row["phase"]
        if to_phase not in PHASE_GRAPH.get(frm, set()):
            raise PhaseConflictError(f"illegal request phase {frm!r}->{to_phase!r}")
        if from_phase is not None and frm != from_phase:
            raise PhaseConflictError(f"expected phase {from_phase!r}, got {frm!r}")
        with self.store.tx() as conn:
            cur = self.store.exec(
                conn,
                "UPDATE requests SET phase = ?, updated_at = ?"
                " WHERE principal_id = ? AND request_id = ? AND phase = ?",
                (to_phase, utc_now(), principal_id, request_id, frm),
            )
            return cur.rowcount == 1

    def mark_prepared(self, principal_id: str, request_id: str) -> bool:
        """Durable intent recorded; dispatch may now be claimed."""
        return self._set_phase(principal_id, request_id, "prepared")

    def claim_dispatch(self, principal_id: str, request_id: str) -> bool:
        """Exactly-once dispatch gate: CAS prepared->sending.
        Returns False (never dispatch) unless this call wins the CAS —
        including from sending/acknowledged/ambiguous, where re-dispatch
        is forbidden (STORAGE.md §3 ④)."""
        row = self.store.get_request(principal_id, request_id)
        if row is None:
            raise NotFoundError(f"request {request_id} not found")
        if row["phase"] != "prepared":
            return False
        with self.store.tx() as conn:
            cur = self.store.exec(
                conn,
                "UPDATE requests SET phase = 'sending', updated_at = ?"
                " WHERE principal_id = ? AND request_id = ? AND phase = 'prepared'",
                (utc_now(), principal_id, request_id),
            )
            return cur.rowcount == 1

    def mark_acknowledged(self, principal_id: str, request_id: str) -> bool:
        return self._set_phase(principal_id, request_id, "acknowledged")

    def mark_completed(self, principal_id: str, request_id: str) -> bool:
        return self._set_phase(principal_id, request_id, "completed")

    def mark_failed(self, principal_id: str, request_id: str) -> bool:
        return self._set_phase(principal_id, request_id, "failed")
