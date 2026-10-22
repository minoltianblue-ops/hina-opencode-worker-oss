"""Operate profiles and fail-closed operate gating (M2-Q3).

WORKSPACE.md ch7: real service/system configuration CANNOT be isolated
by a Worktree, so operate needs a SEPARATE profile, an explicit user
approval record and a recovery plan before anything is even planned.
This module owns the profile (``operate.yaml``: explicit scope +
approval record + recovery plan) and the gate the supervisor consults.

M2 boundary (brief + ch7): this is the PLANNING side only. Dry-run is
the default and the ONLY mode this package implements; there is no code
path here that touches a real service, tunnel, V1 app or cokacremote —
actual service/system changes require a SEPARATE user approval that
does not exist in this package, so they are refused unconditionally
(FORBIDDEN), not merely unconfigured. Fail-closed everywhere: a missing,
unparsable, unapproved or drifted profile refuses everything.

Error codes are a subset of the frozen contract enum (T13); no new
codes are introduced: CAPABILITY_UNVERIFIED, APPROVAL_REQUIRED,
APPROVAL_EXPIRED, APPROVAL_INVALID, FORBIDDEN, STALE_ATTEMPT,
NOT_FOUND, INVALID_ARGUMENT.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from .requests import canonical_json

# profile schema freeze
PROFILE_NAME = "operate"
PROFILE_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
MAX_PROFILE_LINES = 512
MAX_DEPTH = 6

# Explicit action enum — an operate scope may only ever name one of
# these; there is deliberately no "exec"/"shell"/freeform action.
OPERATE_ACTIONS = (
    "service.start", "service.stop", "service.restart",
    "launchagent.load", "launchagent.unload", "config.apply",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# ch8 discipline: explicit targets only — no glob, no directory
# recursion, no whitespace.
_TARGET_RE = re.compile(r"^[^\s\x00-*?\[\]~]{1,256}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_APPROVER_RE = re.compile(r"^user:[A-Za-z0-9._-]{1,64}$")
_REF_RE = re.compile(r"^(?!-)[^\s\x00]{1,1024}$")
_ATT_ID_RE = re.compile(
    r"^att_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")
_RECORD_ID_RE = re.compile(
    r"^opr_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")


class OperateDenied(Exception):
    """Every refusal carries a frozen contract error code."""

    def __init__(self, code: str, message: str, *, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _epoch_z(text: str, *, field: str) -> float:
    try:
        return datetime.strptime(
            text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc
        ).timestamp()
    except (TypeError, ValueError):
        raise OperateDenied("APPROVAL_REQUIRED",
                            f"{field} must be RFC3339 UTC (…Z)")


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# minimal, strict YAML-subset reader (stdlib only; no PyYAML)
# ---------------------------------------------------------------------------
# Supported by construction: block mappings, block sequences, scalar
# values (plain / single / double quoted), true/false/null, integers and
# whole-line comments. Everything else — flow style, anchors, aliases,
# multiline scalars, tabs, duplicate keys, deep nesting — is REFUSED with
# a precise message (fail-closed: an operate profile we cannot read with
# certainty is a profile that approves nothing). Sequences must be
# INDENTED under their key (the shape this repo's writers emit); a
# key-column sequence is refused rather than guessed at.

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UNSUPPORTED_PREFIX = ("&", "*", "|", ">", "%", "@", "`", "{", "[")


def _scalar(raw: str):
    raw = raw.strip()
    if raw == "" or raw in ("null", "~"):
        return None
    if raw[0] in _UNSUPPORTED_PREFIX:
        raise OperateDenied("APPROVAL_REQUIRED",
                            f"unsupported YAML value {raw[:24]!r} "
                            f"(flow style/anchors/multiline are not "
                            f"accepted in operate profiles)")
    if raw[0] in "\"'" and len(raw) > 1 and raw[-1] == raw[0]:
        body = raw[1:-1]
        if raw[0] == '"' and "\\" in body:
            raise OperateDenied("APPROVAL_REQUIRED",
                                "escape sequences are not accepted")
        return body
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    # YAML plain-scalar restrictions inside this subset: a colon only
    # separates a key when followed by a space/end; a '#' only starts a
    # comment when preceded by a space. Anything ambiguous must be
    # quoted — refusing beats guessing for an approval document.
    if ": " in raw or raw.endswith(":") or " #" in raw or raw.startswith(
            "#"):
        raise OperateDenied(
            "APPROVAL_REQUIRED",
            f"plain value {raw[:24]!r} must be quoted when it contains "
            f"': ', a trailing ':' or an inline comment")
    return raw


def parse_simple_yaml(text: str) -> dict:
    """Parse the supported subset; the document root must be a mapping."""
    if len(text.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise OperateDenied("APPROVAL_REQUIRED", "profile too large")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > MAX_PROFILE_LINES:
        raise OperateDenied("APPROVAL_REQUIRED", "profile has too many"
                                                " lines")
    toks: list[tuple[int, str]] = []
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if line[indent:indent + 1] == "\t":
            raise OperateDenied("APPROVAL_REQUIRED",
                                f"line {n}: tab indentation is refused")
        toks.append((indent, stripped, n))

    pos = 0

    def parse_block(indent: int):
        nonlocal pos
        if pos >= len(toks):
            return None
        first_indent, first, lineno = toks[pos]
        if first_indent < indent:
            return None
        if first.startswith("- ") or first == "-":
            return parse_seq(first_indent)
        return parse_map(first_indent)

    def parse_map(indent: int) -> dict:
        nonlocal pos
        out: dict = {}
        while pos < len(toks):
            ind, content, lineno = toks[pos]
            if ind < indent or (ind == indent and content.startswith(
                    "- ")):
                break
            if ind > indent:
                raise OperateDenied(
                    "APPROVAL_REQUIRED",
                    f"line {lineno}: unexpected indentation")
            if ":" not in content:
                raise OperateDenied("APPROVAL_REQUIRED",
                                    f"line {lineno}: expected 'key:'")
            key, _, rest = content.partition(":")
            key = key.strip()
            if not _KEY_RE.match(key):
                raise OperateDenied("APPROVAL_REQUIRED",
                                    f"line {lineno}: bad key {key[:40]!r}")
            if key in out:
                raise OperateDenied("APPROVAL_REQUIRED",
                                    f"line {lineno}: duplicate key {key!r}")
            pos += 1
            if rest.strip():
                out[key] = _scalar(rest)
            else:
                child_indent = toks[pos][0] if pos < len(toks) else -1
                if child_indent <= indent:
                    out[key] = None
                else:
                    out[key] = parse_block(child_indent)
        return out

    def parse_seq(indent: int) -> list:
        nonlocal pos
        out: list = []
        while pos < len(toks):
            ind, content, lineno = toks[pos]
            if ind < indent:
                break          # parent block resumes at a lower indent
            if ind > indent or not (content.startswith("- ")
                                    or content == "-"):
                raise OperateDenied(
                    "APPROVAL_REQUIRED",
                    f"line {lineno}: malformed sequence item")
            body = content[2:].strip() if content.startswith("- ") else ""
            if body and ":" in body and not body.startswith(("\"", "'")):
                # mapping item starting on the dash line
                key, _, rest = body.partition(":")
                key = key.strip()
                if not _KEY_RE.match(key):
                    raise OperateDenied("APPROVAL_REQUIRED",
                                        f"line {lineno}: bad key "
                                        f"{key[:40]!r}")
                item = {key: _scalar(rest) if rest.strip() else None}
                pos += 1
                child_indent = toks[pos][0] if pos < len(toks) else -1
                if item[key] is None and child_indent > indent + 1:
                    item[key] = parse_block(child_indent)
                    child_indent = (toks[pos][0]
                                    if pos < len(toks) else -1)
                while pos < len(toks) and toks[pos][0] >= indent + 2 \
                        and not toks[pos][1].startswith("- "):
                    sub = parse_map(toks[pos][0])
                    for k, v in sub.items():
                        if k in item:
                            raise OperateDenied(
                                "APPROVAL_REQUIRED",
                                f"line {toks[pos][2]}: duplicate key "
                                f"{k!r}")
                        item[k] = v
                out.append(item)
                continue
            pos += 1
            out.append(_scalar(body))
        return out

    doc = parse_block(0)
    if pos != len(toks):
        raise OperateDenied("APPROVAL_REQUIRED",
                            "unparsed trailing content")
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise OperateDenied("APPROVAL_REQUIRED",
                            "profile root must be a mapping")
    return doc


# ---------------------------------------------------------------------------
# profile schema
# ---------------------------------------------------------------------------

def scope_sha256(scope: list[dict]) -> str:
    return hashlib.sha256(
        canonical_json({"scope": scope}).encode("utf-8")).hexdigest()


class OperateProfile:
    """A validated operate.yaml: explicit scope + approval record +
    recovery plan. Construction only via load()/from_text() — an invalid
    profile never exists as an object (fail-closed)."""

    __slots__ = ("scope", "scope_sha", "approved_by", "user_decision_ref",
                 "approved_at_epoch", "expires_at_epoch", "recovery_plan",
                 "file_sha256")

    def __init__(self, *, scope, approved_by, user_decision_ref,
                 approved_at, expires_at, approval_scope_sha,
                 recovery_plan, file_sha256):
        self.scope = tuple(scope)
        self.scope_sha = scope_sha256(scope)
        self.approved_by = approved_by
        self.user_decision_ref = user_decision_ref
        self.approved_at_epoch = approved_at
        self.expires_at_epoch = expires_at
        self.recovery_plan = tuple(recovery_plan)
        self.file_sha256 = file_sha256
        if approval_scope_sha != self.scope_sha:
            # the approval record pins the exact bytes of the scope it
            # approved — any later scope edit is drift, never silent
            raise OperateDenied(
                "APPROVAL_INVALID",
                "approval.scope_sha256 does not match the scope section — "
                "scope changed after approval (new user approval required)")

    def entry_for(self, action: str, target: str) -> dict | None:
        for e in self.scope:
            if e["action"] == action and e["target"] == target:
                return e
        return None

    def approval_view(self) -> dict:
        return {
            "approved_by": self.approved_by,
            "user_decision_ref": self.user_decision_ref,
            "approved_at": _rfc3339(self.approved_at_epoch),
            "expires_at": _rfc3339(self.expires_at_epoch),
            "scope_sha256": self.scope_sha,
        }


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise OperateDenied("APPROVAL_REQUIRED", msg)


def _parse_scope(raw) -> list[dict]:
    _require(isinstance(raw, list) and 1 <= len(raw) <= 64,
             "scope must be a non-empty explicit list (max 64)")
    out, seen = [], set()
    allowed = {"id", "action", "target", "note"}
    for i, e in enumerate(raw):
        _require(isinstance(e, dict), f"scope[{i}] must be a mapping")
        _require(set(e) - allowed == set(),
                 f"scope[{i}] has unknown keys {sorted(set(e)-allowed)}")
        eid, action, target = e.get("id"), e.get("action"), e.get("target")
        _require(isinstance(eid, str) and _ID_RE.match(eid),
                 f"scope[{i}].id invalid")
        _require(eid not in seen, f"scope[{i}].id duplicated")
        seen.add(eid)
        _require(action in OPERATE_ACTIONS,
                 f"scope[{i}].action must be one of {OPERATE_ACTIONS}")
        _require(isinstance(target, str) and _TARGET_RE.match(target),
                 f"scope[{i}].target must be an explicit value (no glob/"
                 f"directory/whitespace)")
        note = e.get("note")
        _require(note is None or (isinstance(note, str)
                                  and 1 <= len(note) <= 512),
                 f"scope[{i}].note invalid")
        out.append({"id": eid, "action": action, "target": target,
                    "note": note or ""})
    return out


def _parse_recovery(raw) -> list[str]:
    _require(isinstance(raw, list) and 1 <= len(raw) <= 64,
             "recovery_plan is required and must be non-empty (ch7: no "
             "recovery plan, no operate)")
    steps = []
    for i, s in enumerate(raw):
        if isinstance(s, dict):
            _require(set(s) == {"step"}, f"recovery_plan[{i}] must be a "
                                         "step string or {{step: …}}")
            s = s["step"]
        _require(isinstance(s, str) and 4 <= len(s) <= 1000,
                 f"recovery_plan[{i}] must be a 4..1000 char step")
        steps.append(s)
    return steps


def _parse_approval(raw) -> dict:
    _require(isinstance(raw, dict),
             "approval record is required — operate without an explicit "
             "user approval record is refused (ch7)")
    allowed = {"approved_by", "user_decision_ref", "approved_at",
               "expires_at", "scope_sha256"}
    _require(set(raw) - allowed == set(),
             f"approval has unknown keys {sorted(set(raw)-allowed)}")
    for k in allowed:
        _require(k in raw, f"approval.{k} is required")
    _require(_APPROVER_RE.match(str(raw["approved_by"])),
             "approval.approved_by must be user:<name>")
    _require(_REF_RE.match(str(raw["user_decision_ref"])),
             "approval.user_decision_ref invalid")
    approved_at = _epoch_z(raw["approved_at"], field="approval.approved_at")
    expires_at = _epoch_z(raw["expires_at"], field="approval.expires_at")
    _require(expires_at > approved_at,
             "approval.expires_at must be after approved_at")
    _require(isinstance(raw["scope_sha256"], str)
             and _SHA_RE.match(raw["scope_sha256"]),
             "approval.scope_sha256 must be 64 hex")
    return {"approved_by": raw["approved_by"],
            "user_decision_ref": raw["user_decision_ref"],
            "approved_at": approved_at, "expires_at": expires_at,
            "scope_sha256": raw["scope_sha256"]}


def from_text(text: str, *, file_sha256: str) -> OperateProfile:
    doc = parse_simple_yaml(text)
    _require(set(doc) - {"profile", "version", "scope", "approval",
                         "recovery_plan"} == set(),
             f"unknown top-level keys {sorted(set(doc) - {'profile', 'version', 'scope', 'approval', 'recovery_plan'})}")
    _require(doc.get("profile") == PROFILE_NAME,
             "profile must be 'operate'")
    _require(doc.get("version") == PROFILE_VERSION,
             f"version must be {PROFILE_VERSION}")
    for k in ("scope", "approval", "recovery_plan"):
        _require(k in doc, f"{k} section is required")
    scope = _parse_scope(doc["scope"])
    approval = _parse_approval(doc["approval"])
    recovery = _parse_recovery(doc["recovery_plan"])
    return OperateProfile(
        scope=scope, approved_by=approval["approved_by"],
        user_decision_ref=approval["user_decision_ref"],
        approved_at=approval["approved_at"],
        expires_at=approval["expires_at"],
        approval_scope_sha=approval["scope_sha256"],
        recovery_plan=recovery, file_sha256=file_sha256)


def load_profile(path: str) -> OperateProfile | None:
    """None only when the file does not exist (capability not
    installed). Any unreadable/unparsable file REFUSES everything —
    a half-read approval is no approval."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise OperateDenied("APPROVAL_REQUIRED",
                            f"operate profile unreadable: {e.strerror}")
    if len(raw) > MAX_PROFILE_BYTES or b"\x00" in raw:
        raise OperateDenied("APPROVAL_REQUIRED",
                            "operate profile too large or binary")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OperateDenied("APPROVAL_REQUIRED",
                            "operate profile must be UTF-8")
    return from_text(text, file_sha256=hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------------------
# argument validation (supervisor operate path)
# ---------------------------------------------------------------------------

def validate_plan_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise OperateDenied("INVALID_ARGUMENT", "arguments must be an"
                                               " object")
    if set(args) - {"operation", "attempt_id", "dry_run"}:
        raise OperateDenied("INVALID_ARGUMENT",
                            f"unknown keys {sorted(set(args) - {'operation', 'attempt_id', 'dry_run'})}")
    op = args.get("operation")
    if not isinstance(op, dict) or set(op) - {"action", "target"} \
            or "action" not in op or "target" not in op:
        raise OperateDenied("INVALID_ARGUMENT",
                            "operation must be {action, target}")
    if op["action"] not in OPERATE_ACTIONS:
        raise OperateDenied("INVALID_ARGUMENT",
                            f"operation.action must be one of "
                            f"{OPERATE_ACTIONS}")
    if not isinstance(op["target"], str) or not _TARGET_RE.match(
            op["target"]):
        raise OperateDenied("INVALID_ARGUMENT",
                            "operation.target must be explicit (no glob/"
                            "directory/whitespace)")
    att = args.get("attempt_id")
    if att is not None and (not isinstance(att, str)
                            or not _ATT_ID_RE.match(att)):
        raise OperateDenied("INVALID_ARGUMENT", "attempt_id pattern")
    dry = args.get("dry_run", True)
    if not isinstance(dry, bool):
        raise OperateDenied("INVALID_ARGUMENT", "dry_run must be boolean")
    if dry is False:
        # The hard boundary (brief): real service/system changes are
        # NEVER executed through this path — they require a separate
        # user approval that does not exist in M2. Not configurable.
        raise OperateDenied(
            "FORBIDDEN",
            "real (non-dry-run) operate changes are never executed "
            "without a separate user approval (WORKSPACE.md ch7); M2 "
            "implements the dry-run plan path only")
    return {"operation": {"action": op["action"], "target": op["target"]},
            "attempt_id": att, "dry_run": True}


def validate_status_args(args: dict) -> dict:
    if not isinstance(args, dict) or args:
        raise OperateDenied("INVALID_ARGUMENT",
                            "operate.status takes no arguments")
    return {}


def validate_revalidate_args(args: dict) -> dict:
    if not isinstance(args, dict) or set(args) != {"record_id",
                                                   "attempt_id"}:
        raise OperateDenied("INVALID_ARGUMENT",
                            "operate.revalidate needs exactly "
                            "{record_id, attempt_id}")
    if not isinstance(args["record_id"], str) or not _RECORD_ID_RE.match(
            args["record_id"]):
        raise OperateDenied("INVALID_ARGUMENT", "record_id pattern")
    if not isinstance(args["attempt_id"], str) or not _ATT_ID_RE.match(
            args["attempt_id"]):
        raise OperateDenied("INVALID_ARGUMENT", "attempt_id pattern")
    return {"record_id": args["record_id"],
            "attempt_id": args["attempt_id"]}


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

class OperateGate:
    """Fail-closed operate gate over one profile file.

    plan(): refuses without an approved scope; dry-run only; when bound
    to an attempt (per-attempt TTL attempt binding) issues a durable
    approval record (JSONL audit journal under the supervisor root)
    bound to exactly that attempt with its own TTL. revalidate():
    attempt mismatch -> STALE_ATTEMPT, TTL passed -> APPROVAL_EXPIRED,
    idempotent while valid. There is no method that executes anything.
    """

    JOURNAL_NAME = "approvals.jsonl"

    def __init__(self, *, profile_path: str, journal_dir: str,
                 time_fn, attempt_ttl_s: float,
                 attempt_is_live=None):
        self.profile_path = profile_path
        self.journal_dir = journal_dir
        self._time = time_fn
        self._attempt_ttl_s = attempt_ttl_s
        self._attempt_is_live = attempt_is_live or (lambda a: False)
        self._records: dict[str, dict] = {}
        self._journal_loaded = False

    # -- profile --------------------------------------------------------
    def _profile(self) -> OperateProfile | None:
        return load_profile(self.profile_path)

    def _load_journal(self) -> None:
        if self._journal_loaded:
            return
        self._journal_loaded = True
        path = os.path.join(self.journal_dir, self.JOURNAL_NAME)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a torn tail line is skipped
                    if rec.get("event") == "issue":
                        r = rec.get("record") or {}
                        rid = r.get("record_id")
                        if isinstance(rid, str) and _RECORD_ID_RE.match(rid):
                            self._records[rid] = r
        except FileNotFoundError:
            pass
        except OSError:
            pass  # journal availability must never widen authority

    def _append_journal(self, rec: dict) -> None:
        os.makedirs(self.journal_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.journal_dir, self.JOURNAL_NAME)
        line = json.dumps({"event": "issue", "record": rec},
                          sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    # -- operations ------------------------------------------------------
    def plan(self, operation: dict, *, attempt_id: str | None) -> dict:
        # dry_run is enforced by validate_plan_args before we get here;
        # reaching this method already means dry-run.
        prof = self._profile()
        if prof is None:
            raise OperateDenied(
                "CAPABILITY_UNVERIFIED",
                "operate profile not installed — real service/system "
                "work requires a separate operate profile + approval + "
                "recovery plan (WORKSPACE.md ch7)")
        now = self._time()
        if now >= prof.expires_at_epoch:
            raise OperateDenied(
                "APPROVAL_EXPIRED",
                "the user approval window for this operate profile has "
                "expired — nothing is approved after the window")
        entry = prof.entry_for(operation["action"], operation["target"])
        if entry is None:
            raise OperateDenied(
                "FORBIDDEN",
                "operation is not inside the explicitly approved scope",
                details={"scope_sha256": prof.scope_sha})
        binding = None
        if attempt_id is not None:
            if not self._attempt_is_live(attempt_id):
                raise OperateDenied(
                    "STALE_ATTEMPT",
                    "operate approval must bind to the currently active,"
                    " non-terminal attempt")
            self._load_journal()
            binding = self._issue(attempt_id, operation, prof, now)
        return {
            "operation": {"action": operation["action"],
                          "target": operation["target"],
                          "scope_id": entry["id"]},
            "dry_run": True,
            "planned_actions": [{
                "seq": 1,
                "action": operation["action"],
                "target": operation["target"],
                "effect": "DRY_RUN",
                "executed": False,
                "note": "no real service/system change is performed;"
                        " real execution needs a separate user approval"
                        " and is not implemented in M2 (ch7)",
            }],
            "recovery_plan": list(prof.recovery_plan),
            "approval": prof.approval_view(),
            "attempt_binding": binding,
            "profile": {"path": self.profile_path,
                        "file_sha256": prof.file_sha256,
                        "scope_sha256": prof.scope_sha},
        }

    def _issue(self, attempt_id: str, operation: dict,
               prof: OperateProfile, now: float) -> dict:
        rec = {
            "record_id": f"opr_{uuid.uuid4()}",
            "attempt_id": attempt_id,
            "action": operation["action"],
            "target": operation["target"],
            "scope_sha256": prof.scope_sha,
            "profile_file_sha256": prof.file_sha256,
            "user_decision_ref": prof.user_decision_ref,
            "issued_at": _rfc3339(now),
            "expires_at": _rfc3339(now + self._attempt_ttl_s),
            "dry_run": True,
        }
        self._records[rec["record_id"]] = rec
        self._append_journal(rec)
        return rec

    def revalidate(self, record_id: str, attempt_id: str) -> dict:
        self._load_journal()
        rec = self._records.get(record_id)
        if rec is None:
            raise OperateDenied("NOT_FOUND",
                                "no operate approval record for that id")
        if rec["attempt_id"] != attempt_id:
            # per-attempt TTL attempt binding: an approval issued for
            # attempt A can never authorize work attributed to B
            raise OperateDenied(
                "STALE_ATTEMPT",
                "operate approval record is bound to a different attempt",
                details={"bound_attempt": rec["attempt_id"],
                         "presented_attempt": attempt_id})
        if self._time() >= _epoch_z(rec["expires_at"],
                                    field="record.expires_at"):
            raise OperateDenied(
                "APPROVAL_EXPIRED",
                "the per-attempt operate approval TTL has passed",
                details={"expires_at": rec["expires_at"]})
        return dict(rec)

    def status(self) -> dict:
        prof = self._profile()
        out = {"installed": prof is not None,
               "profile_path": self.profile_path}
        if prof is not None:
            out.update({
                "approval": prof.approval_view(),
                "scope": [{"id": e["id"], "action": e["action"],
                           "target": e["target"]} for e in prof.scope],
                "recovery_steps": len(prof.recovery_plan),
            })
        self._load_journal()
        out["approval_records"] = len(self._records)
        return out
