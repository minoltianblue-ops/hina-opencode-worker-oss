"""M3 N2 — per-task MCP/skill capability allowlist (D6).

A task may use an MCP server or a skill ONLY if the user granted it
FOR THAT TASK. grant() is the only means of granting (or revoking —
an empty grant revokes everything) and requires a user_decision_ref:
the record of the user's decision for this task's scope (D6: the
allowlist is per-task authorization the user decides, never a blanket
restriction by data category). Default-deny is absolute: with no
active grant, nothing is usable, and unknown or registry-withdrawn
resources stay unusable — an unverifiable resource is never treated
as granted.

Every decision — allow, deny, and grant — is written to an
append-only audit log (sqlite, UPDATE/DELETE blocked by trigger), so
every refusal is auditable with its reason. A refusal raises
CapabilityPolicyError; it never implicitly widens scope
(details["auto_grant"] is always False) and never resolves on its
own: it requires a fresh user decision through grant(), so
details["requires_user_decision"] is always True.

Skill least-privilege: a skill may touch only MCPs that are BOTH
granted to the task AND declared in the skill's own needs — the
intersection, never either side alone. A skill cannot reach an MCP
the task allows but the skill never declared (out-of-scope), and it
cannot use its declared needs to reach an MCP the task was not
granted (no self-escalation).

Stdlib-only; the sqlite connection is thread-bound (one thread owns
a policy instance). Job-state gating (JOB_BUSY etc.) stays in the
supervisor; this module owns capability authorization and decision
auditing only. Wiring into the supervisor/MCP path is a follow-up
package (same boundary as N1).
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

__all__ = ["CapabilityRegistry", "CapabilityPolicyError", "GrantRecord",
           "TaskCapabilities", "DecisionRecord", "CapabilityPolicy"]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_KINDS = ("mcp", "skill")
_MAX_TEXT = 512

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_grants (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    mcps TEXT NOT NULL,
    skills TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    user_decision_ref TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS capability_grants_task_idx
    ON capability_grants (task_id, seq);
CREATE TRIGGER IF NOT EXISTS capability_grants_no_update
BEFORE UPDATE ON capability_grants
BEGIN SELECT RAISE(ABORT, 'capability_grants is append-only'); END;
CREATE TRIGGER IF NOT EXISTS capability_grants_no_delete
BEFORE DELETE ON capability_grants
BEGIN SELECT RAISE(ABORT, 'capability_grants is append-only'); END;
CREATE TABLE IF NOT EXISTS capability_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    event TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    resource_id TEXT NOT NULL DEFAULT '',
    via_skill TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL DEFAULT '',
    grant_id TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capability_audit_task_idx
    ON capability_audit (task_id, seq);
CREATE TRIGGER IF NOT EXISTS capability_audit_no_update
BEFORE UPDATE ON capability_audit
BEGIN SELECT RAISE(ABORT, 'capability_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS capability_audit_no_delete
BEFORE DELETE ON capability_audit
BEGIN SELECT RAISE(ABORT, 'capability_audit is append-only'); END;
"""


class CapabilityPolicyError(Exception):
    """Contract-shaped refusal. A refusal NEVER grants anything on the
    way (details["auto_grant"] is always False) and NEVER clears on
    retry (details["requires_user_decision"] is True): the only path
    forward is a fresh user decision through grant(). Every refusal
    is already in the append-only audit log when this raises."""

    def __init__(self, code, message, *, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        # No capability refusal is retryable by the worker: only the
        # user granting scope (or the resource being re-enabled)
        # changes the answer.
        return False

    def to_error(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": self.details}


def _require_text(value, field: str, *, pattern=None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityPolicyError("INVALID_ARGUMENT",
                                    f"{field} must be a non-empty string")
    v = value.strip()
    if len(v) > _MAX_TEXT:
        raise CapabilityPolicyError("INVALID_ARGUMENT",
                                    f"{field} exceeds {_MAX_TEXT} characters")
    if pattern is not None and not pattern.match(v):
        raise CapabilityPolicyError("INVALID_ARGUMENT",
                                    f"{field} does not match the id shape")
    return v


def _id_set(values, field: str) -> tuple:
    if values is None or isinstance(values, (str, bytes)):
        raise CapabilityPolicyError(
            "INVALID_ARGUMENT",
            f"{field} must be a sequence of resource ids")
    out = tuple(sorted({_require_text(v, f"{field} entry",
                                    pattern=_ID_RE) for v in values}))
    return out


@dataclass(frozen=True)
class GrantRecord:
    """One user scope decision, appended and immutable. The LATEST
    grant is the active allowlist; an empty grant revokes everything."""
    grant_id: str
    task_id: str
    mcps: tuple
    skills: tuple
    granted_by: str
    user_decision_ref: str
    decided_at: str
    note: str
    seq: int

    def as_dict(self) -> dict:
        return {
            "grant_id": self.grant_id, "task_id": self.task_id,
            "mcps": list(self.mcps), "skills": list(self.skills),
            "granted_by": self.granted_by,
            "user_decision_ref": self.user_decision_ref,
            "decided_at": self.decided_at, "note": self.note,
            "seq": self.seq,
        }


@dataclass(frozen=True)
class TaskCapabilities:
    """Read-only effective scope of the active grant. The allows_*
    predicates are pure (no audit); check() is the authority that
    audits. Semantics are the least-privilege intersection: a skill
    reaches an MCP only if the task grants it AND the skill declares
    it; direct MCP use only needs the task grant."""
    task_id: str
    mcps: frozenset
    skills: frozenset
    skill_needs: dict  # skill_id -> tuple of declared mcp ids
    grant_id: str

    def allows_mcp(self, mcp_id: str, *, via=None) -> bool:
        if mcp_id not in self.mcps:
            return False
        if via is None:
            return True
        if via not in self.skills:
            return False
        return mcp_id in self.skill_needs.get(via, ())

    def allows_skill(self, skill_id: str) -> bool:
        return skill_id in self.skills


@dataclass(frozen=True)
class DecisionRecord:
    """One audit entry: event is 'grant', 'allow' or 'deny'."""
    entry_id: str
    task_id: str
    event: str
    kind: str
    resource_id: str
    via_skill: str
    reason_code: str
    grant_id: str
    at: str
    seq: int

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id, "task_id": self.task_id,
            "event": self.event, "kind": self.kind,
            "resource_id": self.resource_id, "via_skill": self.via_skill,
            "reason_code": self.reason_code, "grant_id": self.grant_id,
            "at": self.at, "seq": self.seq,
        }


class CapabilityRegistry:
    """Known MCP servers and skills. A skill declares `needs` — the
    MCP ids it legitimately requires; least-privilege intersects this
    with the task grant, it never expands it. Withdrawal is
    set_enabled(False): granted-but-withdrawn resources then refuse
    at decision time (an unverifiable resource is never usable)."""

    def __init__(self):
        self._mcps = {}
        self._skills = {}

    def add_mcp(self, mcp_id, *, enabled=True):
        mcp_id = _require_text(mcp_id, "mcp_id", pattern=_ID_RE)
        self._mcps[mcp_id] = {"id": mcp_id, "enabled": bool(enabled)}

    def add_skill(self, skill_id, needs=(), *, enabled=True):
        skill_id = _require_text(skill_id, "skill_id", pattern=_ID_RE)
        needs = _id_set(needs, "needs")
        self._skills[skill_id] = {"id": skill_id, "needs": needs,
                                  "enabled": bool(enabled)}

    def set_enabled(self, kind, resource_id, enabled: bool) -> None:
        if kind not in _KINDS:
            raise CapabilityPolicyError("INVALID_ARGUMENT",
                                        "kind must be 'mcp' or 'skill'")
        rid = _require_text(resource_id, "resource_id", pattern=_ID_RE)
        table = self._mcps if kind == "mcp" else self._skills
        if rid not in table:
            raise CapabilityPolicyError(
                "INVALID_ARGUMENT", f"unknown {kind} id {rid!r}")
        table[rid]["enabled"] = bool(enabled)

    def get_mcp(self, mcp_id):
        return self._mcps.get(mcp_id)

    def get_skill(self, skill_id):
        return self._skills.get(skill_id)

    def mcp_rows(self):
        return [dict(self._mcps[k]) for k in sorted(self._mcps)]

    def skill_rows(self):
        return [dict(self._skills[k], needs=list(self._skills[k]["needs"]))
                for k in sorted(self._skills)]


class CapabilityPolicy:
    """Per-task MCP/skill allowlist with default-deny and an
    append-only audit log. registry is any object with get_mcp(id) /
    get_skill(id) returning a row dict or None (CapabilityRegistry is
    the shipped implementation)."""

    def __init__(self, registry, *, db_path=None, clock=time.time):
        self._registry = registry
        self._clock = clock
        self._db = sqlite3.connect(str(db_path) if db_path is not None
                                   else ":memory:")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- granting: the ONLY means of giving or revoking scope ---------

    def grant(self, task_id, *, mcps=(), skills=(), granted_by=None,
              user_decision_ref=None, note="") -> GrantRecord:
        """Record the user's scope decision for this task and make it
        the active allowlist (latest grant wins). Empty sets revoke —
        revoking is also a user decision and is itself audited.
        Unknown or disabled registry ids refuse the grant itself:
        scope can only ever cover resources the registry knows."""
        task = _require_text(task_id, "task_id")
        mcps = _id_set(mcps, "mcps")
        skills = _id_set(skills, "skills")
        who = _require_text(granted_by, "granted_by")
        ref = _require_text(user_decision_ref, "user_decision_ref")
        if not isinstance(note, str):
            raise CapabilityPolicyError("INVALID_ARGUMENT",
                                        "note must be a string")
        note = note.strip()
        if len(note) > 2000:
            raise CapabilityPolicyError("INVALID_ARGUMENT",
                                        "note exceeds 2000 characters")
        for kind, ids in (("mcp", mcps), ("skill", skills)):
            for rid in ids:
                try:
                    self._grant_gate(kind, rid)
                except CapabilityPolicyError as exc:
                    # A refused scope is a refusal: it belongs in the
                    # audit log too, then re-raises unchanged.
                    self._log(task, "deny", kind="grant", resource_id=rid,
                              reason_code=exc.details["reason_code"])
                    raise
        rec = GrantRecord(
            grant_id="cap_" + str(uuid.uuid4()),
            task_id=task, mcps=mcps, skills=skills,
            granted_by=who, user_decision_ref=ref,
            decided_at=self._stamp(), note=note, seq=0)
        cur = self._db.execute(
            "INSERT INTO capability_grants (grant_id, task_id, mcps,"
            " skills, granted_by, user_decision_ref, decided_at, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rec.grant_id, rec.task_id, json.dumps(list(mcps)),
             json.dumps(list(skills)), who, ref, rec.decided_at, note))
        self._log(task, "grant", grant_id=rec.grant_id)
        return GrantRecord(**{**rec.as_dict(), "seq": cur.lastrowid})

    # -- records -------------------------------------------------------

    def active_grant(self, task_id) -> GrantRecord | None:
        """The active allowlist: the user's latest grant for the task.
        None means default-deny: nothing is usable."""
        task = _require_text(task_id, "task_id")
        row = self._db.execute(
            "SELECT * FROM capability_grants WHERE task_id = ?"
            " ORDER BY seq DESC LIMIT 1", (task,)).fetchone()
        return self._to_grant(row)

    def grants(self, task_id) -> tuple:
        """Append-only grant trail for the task, oldest first."""
        task = _require_text(task_id, "task_id")
        rows = self._db.execute(
            "SELECT * FROM capability_grants WHERE task_id = ?"
            " ORDER BY seq", (task,)).fetchall()
        return tuple(self._to_grant(r) for r in rows if r is not None)

    def plan(self, task_id) -> TaskCapabilities | None:
        """Read-only effective scope (pure view; no audit). None =
        default-deny. check() re-verifies against the registry and
        audits; this only tells a caller what the grant covers."""
        g = self.active_grant(task_id)
        if g is None:
            return None
        needs = {}
        for skill_id in g.skills:
            row = self._registry.get_skill(skill_id)
            needs[skill_id] = tuple(row["needs"]) if row else ()
        return TaskCapabilities(
            task_id=g.task_id, mcps=frozenset(g.mcps),
            skills=frozenset(g.skills), skill_needs=needs,
            grant_id=g.grant_id)

    # -- decision: the auditing authority ------------------------------

    def check(self, task_id, kind, resource_id, *, via=None) -> DecisionRecord:
        """The single decision point for a task touching an MCP
        ('mcp') or a skill ('skill'); via names the skill through
        which an MCP call travels. Every outcome — allow and deny —
        lands in the audit log BEFORE returning/raising. Denials
        raise; they never grant implicitly and never auto-resolve.
        A withdrawal after granting still refuses (registry is
        re-verified at decision time)."""
        task = _require_text(task_id, "task_id")
        if kind not in _KINDS:
            raise CapabilityPolicyError(
                "INVALID_ARGUMENT",
                "kind must be 'mcp' or 'skill'")
        rid = _require_text(resource_id, "resource_id", pattern=_ID_RE)
        if via is not None:
            via = _require_text(via, "via", pattern=_ID_RE)
            if kind != "mcp":
                raise CapabilityPolicyError(
                    "INVALID_ARGUMENT",
                    "via applies only to kind 'mcp'")
        g = self.active_grant(task)
        if g is None:
            self._refuse(task, kind, rid, via=via,
                         reason="no_active_grant")
        self._decision_gate(task, kind, rid, g, via)
        return self._log(task, "allow", kind=kind, resource_id=rid,
                         via_skill=via, grant_id=g.grant_id)

    # -- audit trail -----------------------------------------------------

    def audit(self, task_id) -> tuple:
        """Every decision for the task, oldest first — grants, allows
        and denials. Append-only at the storage level."""
        task = _require_text(task_id, "task_id")
        rows = self._db.execute(
            "SELECT * FROM capability_audit WHERE task_id = ?"
            " ORDER BY seq", (task,)).fetchall()
        return tuple(self._to_decision(r) for r in rows)

    def refusals(self, task_id) -> tuple:
        """Denials only, oldest first — every refusal is here."""
        return tuple(d for d in self.audit(task_id) if d.event == "deny")

    # -- internals -------------------------------------------------------

    def _stamp(self) -> str:
        return datetime.datetime.fromtimestamp(
            self._clock(), datetime.timezone.utc
        ).isoformat(timespec="seconds")

    def _grant_gate(self, kind: str, rid: str) -> None:
        row = (self._registry.get_mcp(rid) if kind == "mcp"
               else self._registry.get_skill(rid))
        if row is None or not row.get("enabled", False):
            raise CapabilityPolicyError(
                "CAPABILITY_DENIED",
                f"{kind} {rid!r} is not a known, enabled registry id;"
                " scope cannot cover an unverifiable resource"
                " (default-deny)",
                details={"disposition": "refused",
                         "reason_code": "registry_unknown_or_disabled",
                         "kind": kind, "resource_id": rid,
                         "requires_user_decision": False,
                         "auto_grant": False})

    def _decision_gate(self, task, kind, rid, g, via) -> None:
        # Allowlist membership first (default-deny names the honest
        # reason for anything off-list, known or not), then the
        # registry — granted-but-withdrawn is its own reason.
        if rid not in (g.mcps if kind == "mcp" else g.skills):
            self._refuse(task, kind, rid, via=via,
                         reason=("mcp_not_allowed" if kind == "mcp"
                                 else "skill_not_allowed"),
                         grant_id=g.grant_id)
        row = (self._registry.get_mcp(rid) if kind == "mcp"
               else self._registry.get_skill(rid))
        if row is None or not row.get("enabled", False):
            self._refuse(task, kind, rid, via=via,
                         reason="registry_withdrawn",
                         grant_id=g.grant_id)
        if kind != "mcp" or via is None:
            return
        # least-privilege: the call travels through a skill, so BOTH
        # the skill grant and the skill's own declared scope apply.
        if via not in g.skills:
            self._refuse(task, "skill", rid, via=via,
                         reason="skill_not_allowed",
                         grant_id=g.grant_id)
        vrow = self._registry.get_skill(via)
        if vrow is None or not vrow.get("enabled", False):
            self._refuse(task, "skill", rid, via=via,
                         reason="registry_withdrawn",
                         grant_id=g.grant_id)
        if rid not in vrow["needs"]:
            self._refuse(task, "mcp", rid, via=via,
                         reason="skill_out_of_scope",
                         grant_id=g.grant_id)

    def _refuse(self, task, kind, rid, *, via=None, reason,
                grant_id="") -> None:
        self._log(task, "deny", kind=kind, resource_id=rid, via_skill=via,
                  reason_code=reason, grant_id=grant_id)
        raise CapabilityPolicyError(
            "CAPABILITY_DENIED",
            f"task {task!r} may not use {kind} {rid!r}"
            + (f" via skill {via!r}" if via else "")
            + f" ({reason}); scope changes only through grant() with"
              " a fresh user decision (D6)",
            details={"disposition": "refused", "reason_code": reason,
                     "task_id": task, "kind": kind, "resource_id": rid,
                     "via_skill": via or "",
                     "requires_user_decision": True,
                     "auto_grant": False})

    def _log(self, task, event, *, kind="", resource_id="", via_skill=None,
             reason_code="", grant_id="") -> DecisionRecord:
        cur = self._db.execute(
            "INSERT INTO capability_audit (entry_id, task_id, event, kind,"
            " resource_id, via_skill, reason_code, grant_id, at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("aud_" + str(uuid.uuid4()), task, event, kind,
             resource_id, via_skill or "", reason_code, grant_id,
             self._stamp()))
        self._db.commit()
        return self._to_decision(
            self._db.execute("SELECT * FROM capability_audit"
                             " WHERE seq = ?", (cur.lastrowid,)).fetchone())

    def _to_grant(self, row) -> GrantRecord | None:
        if row is None:
            return None
        return GrantRecord(
            grant_id=row["grant_id"], task_id=row["task_id"],
            mcps=tuple(json.loads(row["mcps"])),
            skills=tuple(json.loads(row["skills"])),
            granted_by=row["granted_by"],
            user_decision_ref=row["user_decision_ref"],
            decided_at=row["decided_at"], note=row["note"],
            seq=row["seq"])

    def _to_decision(self, row) -> DecisionRecord:
        return DecisionRecord(
            entry_id=row["entry_id"], task_id=row["task_id"],
            event=row["event"], kind=row["kind"],
            resource_id=row["resource_id"], via_skill=row["via_skill"],
            reason_code=row["reason_code"], grant_id=row["grant_id"],
            at=row["at"], seq=row["seq"])
