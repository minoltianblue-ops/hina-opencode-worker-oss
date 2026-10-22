"""Local supervisor — owns the run lifecycle (M1 P4).

Single current task (PLAN §4 / M1_WORK_PACKAGES P4): one active attempt;
other writers are refused with BUSY and no auto queue is created.

Roles (MCP.md §1): the stdio proxy is a separate process; this module
owns jobs/attempts/state. The supervisor listens on a Unix domain
socket under <root>/run/supervisor.sock (parent 0700, socket 0600) and
verifies the peer's UID with getpeereid on every connection. The
principal is assigned server-side as ``local:<uid>`` — clients never
supply a principal field.

Backend is injected (BackendProtocol below). Tests use a scripted fake
(mocking is allowed until PI integration); PI wires the real
OpenCodeV2Adapter/capture path on the Mac mini. P1 (store/state/
requests), P2 (opencode_v2/events) and P3 (workspace/artifacts) files
are consumed unmodified — this module only adds rows through the
shared schema.

Error model: every failure leaves the tool boundary as a contract
error envelope (docs/contracts/mcp-tools.json error enum). Unexpected
exceptions never escape as raw tracebacks (INTERNAL, sanitized).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from . import artifacts as art
from . import operate as opmod
from . import workspace as ws
from .events import (
    KIND_MESSAGE,
    VERDICT_CLI_FAILED,
    VERDICT_CLI_OK_TOOL_FAILED,
    VERDICT_COMPLETED,
    VERDICT_CONTRACT_MISMATCH,
    VERDICT_EMPTY_OUTPUT,
    VERDICT_NO_FINAL_ANSWER,
    VERDICT_UNKNOWN,
)
from .opencode_v2 import (
    ApiResponse,
    ApprovalError,
    ApprovalGrant,
    ApprovalJournal,
    CancelResult,
    ContractMismatch,
    DELIVERY_ENUM,
    compute_challenge,
    validate_and_reply,
)
from .requests import (
    BusyError,
    IdempotencyConflictError,
    InvalidRequestError,
    RequestJournal,
    canonical_json,
    payload_sha256,
)
from .state import InvariantFailureError, StateMachine
from .store import (
    CasConflictError,
    DatabaseLockedError,
    LeaseHeldError,
    NotFoundError,
    StorageFullError,
    Store,
    SupervisorLock,
    new_id,
    utc_now,
)

CONTRACT_VERSION = "0.3"
MAX_INPUT_BYTES = 256 * 1024          # §5: input envelope
MAX_RESPONSE_BYTES = 256 * 1024       # §5: serialized response (incl. dup text)
WAIT_MS_DEFAULT = 20000
WAIT_MS_MAX = 25000
APPROVAL_TTL_S = 15 * 60              # §7: 15 min technical-token TTL
OPERATE_ATTEMPT_TTL_S = 10 * 60       # Q3/ch7: per-attempt operate TTL
OPERATE_METHODS = ("operate.plan", "operate.status", "operate.revalidate")
CURSOR_TTL_S = 24 * 3600              # §5
CONTROL_TIMEOUT_S = 5.0               # §4: backend control default budget
CONTROL_CALLS_MAX = 1                 # §4: one attempt, no auto re-send
WAIT_POLL_S = 0.1                     # §4: cancel must be acceptable ~1 s
APPROVAL_POLL_S = 0.2
MODEL_PROBE_POLL_S = 1.0

JOB_STATES = (
    "accepted", "starting", "running", "collecting", "waiting_approval",
    "waiting_model_selection", "cancel_requested", "completed", "failed",
    "cancelled", "needs_attention",
)
ACTIVE_JOB_STATES = frozenset(
    s for s in JOB_STATES if s not in ("completed", "failed", "cancelled"))
TERMINAL_JOB_STATES = frozenset(("completed", "failed", "cancelled"))

# docs/contracts/mcp-tools.json error enum — single source checked by T13.
ERROR_CODES = (
    "INVALID_ARGUMENT", "UNAUTHENTICATED", "FORBIDDEN", "NOT_FOUND",
    "IDEMPOTENCY_CONFLICT", "BUSY", "JOB_BUSY", "STALE_ATTEMPT",
    "STALE_RESULT", "MODEL_NOT_ALLOWED", "MODEL_UNAVAILABLE",
    "CONTRACT_MISMATCH", "CAPABILITY_UNVERIFIED", "APPROVAL_REQUIRED",
    "APPROVAL_INVALID", "APPROVAL_EXPIRED", "APPROVAL_CHANNEL_UNAVAILABLE",
    "WORKSPACE_DIRTY", "WORKSPACE_CHANGED", "WORKSPACE_CONFLICT",
    "CURSOR_INVALID", "CURSOR_EXPIRED", "BACKEND_UNAVAILABLE",
    "BACKEND_TIMEOUT", "DISPATCH_AMBIGUOUS", "CANCEL_UNCONFIRMED",
    "EVIDENCE_INCOMPLETE", "STORAGE_FULL", "INTERNAL", "RESULT_NOT_READY",
)
RETRYABLE = frozenset((
    "BUSY", "JOB_BUSY", "BACKEND_UNAVAILABLE", "BACKEND_TIMEOUT",
    "CANCEL_UNCONFIRMED", "STORAGE_FULL", "MODEL_UNAVAILABLE",
))

_REQ_ID_RE = re.compile(
    r"^req_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_JOB_ID_RE = re.compile(
    r"^job_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")
_ATT_ID_RE = re.compile(
    r"^att_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")
_ART_ID_RE = re.compile(
    r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")
_APR_ID_RE = re.compile(
    r"^apr_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")
_MODEL_RE = re.compile(r"^[^\s/]+/[^\s]+$")
_REF_RE = re.compile(r"^(?!-)[^\s\x00]+$")


def _job_id_field(args, key="job_id"):
    v = _str_field(args, key, max_len=256)
    if not _JOB_ID_RE.match(v):
        raise WorkerError("INVALID_ARGUMENT", f"{key} pattern")
    return v


class WorkerError(Exception):
    """Contract error raised inside tool handlers, never escapes the
    boundary raw — handle() converts it to an error envelope."""

    def __init__(self, code: str, message: str, *,
                 details: dict | None = None) -> None:
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


# Backend-facing exceptions (supervisor-local classification boundary)
class BackendUnavailable(Exception):
    pass


class BackendTimeout(Exception):
    pass


class ModelUnavailableError(Exception):
    pass


# ---------------------------------------------------------------------------
# catalog — D2/D6: no inference, no auto-switch, unknown cost != 0
# ---------------------------------------------------------------------------

class ModelCatalog:
    """Approved models only. billing_class is NEVER inferred: an unknown
    class stays 'unknown' and its cost_state stays 'unknown' — never
    silently treated as free/zero-cost (T17)."""

    def __init__(self, models=None, *, profiles=("inspect", "develop"),
                 gate="PASS", engine=None, capabilities=None):
        self._models = {}
        for m in models or ():
            self._models[m["id"]] = dict(m)
        self.profiles = list(profiles)
        self.gate = gate
        self.engine = engine or {"version": "unprobed",
                                 "binary_sha256": None,
                                 "mapping_revision": None}
        self.capabilities = dict(capabilities or {})
        if any(not isinstance(key, str) or type(value) is not bool
               for key, value in self.capabilities.items()):
            raise ValueError("catalog capabilities must map names to booleans")

    def add(self, model_id, billing_class, *, enabled=True,
            probe_status="unverified"):
        assert billing_class in ("free", "go", "unknown")
        assert probe_status in ("unverified", "help_only", "live_verified",
                                "unavailable")
        self._models[model_id] = {
            "id": model_id, "billing_class": billing_class,
            "enabled": bool(enabled), "probe_status": probe_status}

    def get(self, model_id):
        return self._models.get(model_id)

    def rows(self):
        return [dict(self._models[k]) for k in sorted(self._models)]

    def billing_class(self, model_id: str) -> str:
        m = self._models.get(model_id)
        return m["billing_class"] if m else "unknown"

    def check(self, model_id: str) -> None:
        """Start/selection gate. Raises WorkerError."""
        m = self._models.get(model_id)
        if m is None or not m["enabled"]:
            raise WorkerError("MODEL_NOT_ALLOWED",
                              f"model {model_id!r} is not an approved,"
                              " enabled catalog id")
        if m["probe_status"] == "unavailable":
            raise WorkerError("MODEL_UNAVAILABLE",
                              f"model {model_id!r} is currently marked"
                              " unavailable", details={"billing_class": m["billing_class"]})

    def available(self, model_id: str) -> bool:
        m = self._models.get(model_id)
        return bool(m and m["enabled"] and m["probe_status"] != "unavailable")


# ---------------------------------------------------------------------------
# input validation — hand-checked against docs/contracts/mcp-tools.json
# ---------------------------------------------------------------------------

def _need(args: dict, key: str) -> None:
    if key not in args:
        raise WorkerError("INVALID_ARGUMENT", f"missing required field {key!r}")


def _type_ok(value, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    return False


def _str_field(args: dict, key: str, *, optional=False, min_len=1,
               max_len=None, pattern=None, enum=None):
    if key not in args:
        if optional:
            return None
        raise WorkerError("INVALID_ARGUMENT", f"missing required field {key!r}")
    v = args[key]
    if not isinstance(v, str):
        raise WorkerError("INVALID_ARGUMENT", f"{key!r} must be a string")
    if len(v) < min_len or (max_len is not None and len(v) > max_len):
        raise WorkerError("INVALID_ARGUMENT", f"{key!r} length out of range")
    if pattern is not None and not pattern.match(v):
        raise WorkerError("INVALID_ARGUMENT", f"{key!r} violates pattern")
    if enum is not None and v not in enum:
        raise WorkerError("INVALID_ARGUMENT",
                          f"{key!r} must be one of {sorted(enum)}")
    return v


def _no_extras(args: dict, allowed: set) -> None:
    for k in args:
        if k not in allowed:
            raise WorkerError("INVALID_ARGUMENT",
                              f"unknown property {k!r} (unspecified"
                              " properties are refused)")


def _str_list(args, key, *, required, min_items=0, max_items=None):
    if key not in args:
        if required:
            raise WorkerError("INVALID_ARGUMENT",
                              f"missing required field {key!r}")
        return []
    v = args[key]
    if not isinstance(v, list) or any(not isinstance(x, str) or not x
                                      or len(x) > 2000 for x in v):
        raise WorkerError("INVALID_ARGUMENT",
                          f"{key!r} must be a list of strings (1..2000)")
    if len(v) < min_items or (max_items is not None and len(v) > max_items):
        raise WorkerError("INVALID_ARGUMENT", f"{key!r} count out of range")
    return v


def _check_request_id(args) -> str:
    return _str_field(args, "request_id", pattern=_REQ_ID_RE)


def validate_start(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"request_id", "user_request_ref", "goal", "workspace",
                      "profile", "model", "constraints", "done_when",
                      "context", "verification_commands"})
    rid = _check_request_id(args)
    _str_field(args, "user_request_ref", max_len=1024)
    _str_field(args, "goal", max_len=12000)
    ws_in = args.get("workspace")
    if not isinstance(ws_in, dict):
        raise WorkerError("INVALID_ARGUMENT", "'workspace' must be an object")
    _no_extras(ws_in, {"repository", "source_ref", "include_existing_changes",
                       "context_files", "use_committed_only"})
    _str_field(ws_in, "repository", max_len=4096)
    if "source_ref" in ws_in:
        _str_field(ws_in, "source_ref", max_len=512, pattern=_REF_RE)
    inc = bool(ws_in.get("include_existing_changes", False))
    ctx_files = ws_in.get("context_files", [])
    if not isinstance(ctx_files, list) or any(
            not isinstance(p, str) or not p or len(p) > 4096
            for p in ctx_files) or len(ctx_files) > 200:
        raise WorkerError("INVALID_ARGUMENT", "context_files invalid")
    if inc and not ctx_files:
        raise WorkerError(
            "INVALID_ARGUMENT",
            "include_existing_changes=true requires non-empty context_files")
    if inc and ws_in.get("use_committed_only") is True:
        raise WorkerError(
            "INVALID_ARGUMENT",
            "include_existing_changes=true conflicts with"
            " use_committed_only=true")
    committed_only = bool(ws_in.get("use_committed_only", False))
    profile = _str_field(args, "profile", enum=("inspect", "develop"))
    model = _str_field(args, "model", max_len=512, pattern=_MODEL_RE)
    constraints = _str_list(args, "constraints", required=True, max_items=50)
    done_when = _str_list(args, "done_when", required=True, min_items=1,
                          max_items=50)
    context = _str_field(args, "context", optional=True, min_len=0,
                         max_len=24000) or ""
    vcs_in = args.get("verification_commands", [])
    if not isinstance(vcs_in, list) or len(vcs_in) > 20:
        raise WorkerError("INVALID_ARGUMENT", "verification_commands invalid")
    vcs = []
    for vc in vcs_in:
        if not isinstance(vc, dict):
            raise WorkerError("INVALID_ARGUMENT",
                              "verification_commands entries must be objects")
        _no_extras(vc, {"argv", "cwd_relative", "timeout_ms"})
        argv = vc.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(a, str) or len(a) > 4096 for a in argv)
                or len(argv) > 128):
            raise WorkerError("INVALID_ARGUMENT", "verification argv invalid")
        cwd_rel = vc.get("cwd_relative", ".")
        if not isinstance(cwd_rel, str) or not cwd_rel or len(cwd_rel) > 1024:
            raise WorkerError("INVALID_ARGUMENT", "cwd_relative invalid")
        t_ms = vc.get("timeout_ms", 3_600_000)
        if not isinstance(t_ms, int) or isinstance(t_ms, bool) \
                or not (1 <= t_ms <= 3_600_000):
            raise WorkerError("INVALID_ARGUMENT", "timeout_ms out of range")
        vcs.append({"argv": list(argv), "cwd_relative": cwd_rel,
                    "timeout_ms": t_ms})
    if profile == "inspect" and vcs:
        raise WorkerError(
            "INVALID_ARGUMENT",
            "profile inspect forbids verification_commands"
            " (read-only profile: shell tests/edits are not permitted)")
    return {"request_id": rid, "user_request_ref": args["user_request_ref"],
            "goal": args["goal"], "workspace": dict(ws_in),
            "include_existing_changes": inc, "context_files": list(ctx_files),
            "committed_only": committed_only, "profile": profile,
            "model": model, "constraints": constraints,
            "done_when": done_when, "context": context,
            "verification_commands": vcs}


def validate_status(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"job_id", "states", "limit", "cursor"})
    out = {}
    if "job_id" in args:
        _str_field(args, "job_id", max_len=256)
        if not _JOB_ID_RE.match(args["job_id"]):
            raise WorkerError("INVALID_ARGUMENT", "job_id pattern")
        if any(k in args for k in ("states", "cursor", "limit")):
            raise WorkerError(
                "INVALID_ARGUMENT",
                "single-job lookup cannot be mixed with list filters")
        out["job_id"] = args["job_id"]
        return out
    if "states" in args:
        st = args["states"]
        if (not isinstance(st, list) or not 1 <= len(st) <= 11
                or any(s not in JOB_STATES for s in st)):
            raise WorkerError("INVALID_ARGUMENT", "states filter invalid")
        out["states"] = list(st)
    if "limit" in args:
        limit = args["limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) \
                or not (1 <= limit <= 100):
            raise WorkerError("INVALID_ARGUMENT", "limit must be 1..100")
        out["limit"] = limit
    else:
        out["limit"] = 20
    if "cursor" in args:
        _str_field(args, "cursor", max_len=4096)
        out["cursor"] = args["cursor"]
    return out


def validate_wait(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"job_id", "wait_ms"})
    job_id = _str_field(args, "job_id", max_len=256)
    if not _JOB_ID_RE.match(job_id):
        raise WorkerError("INVALID_ARGUMENT", "job_id pattern")
    wait_ms = args.get("wait_ms", WAIT_MS_DEFAULT)
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) \
            or not (0 <= wait_ms <= WAIT_MS_MAX):
        raise WorkerError("INVALID_ARGUMENT",
                          f"wait_ms must be an integer 0..{WAIT_MS_MAX}")
    return {"job_id": job_id, "wait_ms": wait_ms}


_RESULT_SECTIONS = ("summary", "checks", "changes", "artifacts", "content")


def validate_result(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"job_id", "attempt_id", "section", "artifact_id",
                      "limit", "byte_limit", "cursor"})
    job_id = _str_field(args, "job_id", max_len=256)
    if not _JOB_ID_RE.match(job_id):
        raise WorkerError("INVALID_ARGUMENT", "job_id pattern")
    out = {"job_id": job_id}
    section = args.get("section", "summary")
    if section not in _RESULT_SECTIONS:
        raise WorkerError("INVALID_ARGUMENT", "section enum invalid")
    out["section"] = section
    aid = args.get("artifact_id")
    if section == "content":
        if aid is None:
            raise WorkerError(
                "INVALID_ARGUMENT",
                "section content requires artifact_id")
        if not isinstance(aid, str) or not _ART_ID_RE.match(aid):
            raise WorkerError("INVALID_ARGUMENT", "artifact_id pattern")
        out["artifact_id"] = aid
    elif aid is not None:
        raise WorkerError("INVALID_ARGUMENT",
                          f"artifact_id is only valid with section content")
    if "attempt_id" in args:
        att = args["attempt_id"]
        if not isinstance(att, str) or not _ATT_ID_RE.match(att):
            raise WorkerError("INVALID_ARGUMENT", "attempt_id pattern")
        out["attempt_id"] = att
    limit = args.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) \
            or not (1 <= limit <= 100):
        raise WorkerError("INVALID_ARGUMENT", "limit must be 1..100")
    out["limit"] = limit
    b_limit = args.get("byte_limit", 16384)
    if not isinstance(b_limit, int) or isinstance(b_limit, bool) \
            or not (1 <= b_limit <= 65536):
        raise WorkerError("INVALID_ARGUMENT",
                          "byte_limit must be 1..65536")
    out["byte_limit"] = b_limit
    if "cursor" in args:
        _str_field(args, "cursor", max_len=4096)
        out["cursor"] = args["cursor"]
    return out


def validate_followup(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"request_id", "user_request_ref", "job_id",
                      "expected_attempt_id", "mode", "message", "model",
                      "user_decision_ref", "delivery"})
    rid = _check_request_id(args)
    _str_field(args, "user_request_ref", max_len=1024)
    job_id = _str_field(args, "job_id", max_len=256)
    if not _JOB_ID_RE.match(job_id):
        raise WorkerError("INVALID_ARGUMENT", "job_id pattern")
    exp_att = _str_field(args, "expected_attempt_id", max_len=256)
    if not _ATT_ID_RE.match(exp_att):
        raise WorkerError("INVALID_ARGUMENT", "expected_attempt_id pattern")
    mode = _str_field(args, "mode", enum=("continue", "retry", "select_model"))
    _str_field(args, "message", max_len=24000)
    out = {"request_id": rid, "user_request_ref": args["user_request_ref"],
           "job_id": job_id, "expected_attempt_id": exp_att, "mode": mode,
           "message": args["message"]}
    has_model = "model" in args
    has_decision = "user_decision_ref" in args
    if mode == "select_model":
        if not has_model or not has_decision:
            raise WorkerError(
                "INVALID_ARGUMENT",
                "select_model requires model and user_decision_ref")
        out["model"] = _str_field(args, "model", max_len=512,
                                  pattern=_MODEL_RE)
        out["user_decision_ref"] = _str_field(args, "user_decision_ref",
                                              max_len=1024)
    elif has_model or has_decision:
        raise WorkerError(
            "INVALID_ARGUMENT",
            f"mode {mode!r} forbids model/user_decision_ref")
    if "delivery" in args:
        # Q2 explicit queued followup (coordinator-frozen shape): the
        # observed delivery enum, admitted ONLY on mode continue while
        # the job runs (state rule lives in the tool; shape rule here).
        _str_field(args, "delivery", enum=DELIVERY_ENUM)
        if mode != "continue":
            raise WorkerError(
                "INVALID_ARGUMENT",
                "delivery is only valid with mode continue (steer/queue"
                " target a running job)")
        out["delivery"] = args["delivery"]
    return out


def validate_cancel(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"request_id", "job_id", "expected_attempt_id",
                      "reason"})
    rid = _check_request_id(args)
    job_id = _str_field(args, "job_id", max_len=256)
    if not _JOB_ID_RE.match(job_id):
        raise WorkerError("INVALID_ARGUMENT", "job_id pattern")
    exp = _str_field(args, "expected_attempt_id", max_len=256)
    if not _ATT_ID_RE.match(exp):
        raise WorkerError("INVALID_ARGUMENT", "expected_attempt_id pattern")
    reason = _str_field(args, "reason", optional=True, max_len=1000)
    return {"request_id": rid, "job_id": job_id,
            "expected_attempt_id": exp, "reason": reason}


def validate_approve(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"request_id", "job_id", "attempt_id", "approval_id",
                      "challenge", "decision", "user_decision_ref"})
    rid = _check_request_id(args)
    job_id = _str_field(args, "job_id", max_len=256)
    if not _JOB_ID_RE.match(job_id):
        raise WorkerError("INVALID_ARGUMENT", "job_id pattern")
    att = _str_field(args, "attempt_id", max_len=256)
    if not _ATT_ID_RE.match(att):
        raise WorkerError("INVALID_ARGUMENT", "attempt_id pattern")
    apr = _str_field(args, "approval_id", max_len=256)
    if not _APR_ID_RE.match(apr):
        raise WorkerError("INVALID_ARGUMENT", "approval_id pattern")
    chal = _str_field(args, "challenge", min_len=43, max_len=128)
    if not re.match(r"^[A-Za-z0-9_-]+$", chal):
        raise WorkerError("INVALID_ARGUMENT", "challenge charset")
    decision = _str_field(args, "decision", enum=("approve", "deny"))
    udr = _str_field(args, "user_decision_ref", max_len=1024)
    return {"request_id": rid, "job_id": job_id, "attempt_id": att,
            "approval_id": apr, "challenge": chal, "decision": decision,
            "user_decision_ref": udr}


def validate_catalog(args: dict) -> dict:
    if not isinstance(args, dict):
        raise WorkerError("INVALID_ARGUMENT", "input must be a JSON object")
    _no_extras(args, {"refresh"})
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        raise WorkerError("INVALID_ARGUMENT", "refresh must be boolean")
    return {"refresh": refresh}


TOOL_VALIDATORS = {
    "worker_catalog": validate_catalog,
    "worker_start": validate_start,
    "worker_status": validate_status,
    "worker_wait": validate_wait,
    "worker_result": validate_result,
    "worker_followup": validate_followup,
    "worker_cancel": validate_cancel,
    "worker_approve": validate_approve,
}
STATE_CHANGE_TOOLS = ("worker_start", "worker_followup", "worker_cancel",
                      "worker_approve")
MUTATING_TOOLS = STATE_CHANGE_TOOLS  # readOnlyHint=false in tools/list


# ---------------------------------------------------------------------------
# backend protocol + live wiring (thin; PI exercises on Mac mini)
# ---------------------------------------------------------------------------

class BackendRun:
    def __init__(self, session_id: str, handle=None):
        self.session_id = session_id
        self.handle = handle


def _final_answer_text(events) -> str | None:
    """Verbatim text of the trailing final assistant message — the same
    trailing-message rule judge_completion uses for COMPLETED (M0-01
    golden: the last event of a real run is one text part).

    Consecutive snapshots of one text part (streamed growth, kept by the
    normalizer as dup_conflict) collapse to the LATEST occurrence per
    part id; distinct trailing parts join in stream order without
    inserting characters the model never wrote. Returns None when the
    stream does not end on message text — never fabricated."""
    parts: list[str] = []              # newest-first kept texts
    seen: set = set()                  # part ids whose newest text kept
    for ev in reversed(events or []):
        if ev.kind != KIND_MESSAGE:
            break                      # stream not at a final message
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            continue                   # empty snapshot: keep scanning
        env = payload.get("envelope")
        part = env.get("part") if isinstance(env, dict) else None
        pid = part.get("id") if isinstance(part, dict) else None
        if pid in seen:
            continue
        seen.add(pid)
        parts.append(text)
    if not parts:
        return None
    return "".join(reversed(parts))


class AttemptOutcome:
    def __init__(self, verdict, reason, *, usage="unknown", session_id=None,
                 model_unavailable=False, raw_stdout=b"",
                 permission_request=None, exit_code=None, answer_text=None):
        self.verdict = verdict              # events.py VERDICT_* values
        self.reason = reason
        self.usage = usage
        self.session_id = session_id
        self.model_unavailable = model_unavailable
        self.raw_stdout = raw_stdout
        self.permission_request = permission_request
        self.exit_code = exit_code
        # Verbatim final assistant message text (None = no trailing
        # message; never fabricated). Feeds summary so the connector
        # always sees the answer (M4-A3 round-trip delivery fix).
        self.answer_text = answer_text


class SupervisorBackend:
    """Protocol actually used by the runner. Fakes implement the same
    surface; LiveBackend below wires the P2 adapter."""

    def probe(self) -> dict:
        raise NotImplementedError

    def available(self, model: str) -> bool:
        raise NotImplementedError

    def start(self, *, job_id, attempt_id, model, cwd, prompt) -> BackendRun:
        raise NotImplementedError

    def wait(self, run: BackendRun, *, cancel: threading.Event,
             deadline_s: float,
             on_permission) -> AttemptOutcome:
        raise NotImplementedError

    def reconcile(self, session_id: str, timeout_s: float) -> str:
        raise NotImplementedError

    def prompt(self, session_id: str, text: str, delivery: str):
        """Q2 busy followup delivery via the observed prompt API;
        delivery comes from the observed enum [steer, queue]. A backend
        that cannot deliver a busy prompt raises NotImplementedError —
        the caller surfaces BACKEND_UNAVAILABLE, never a silent drop."""
        raise NotImplementedError

    def pending_permissions(self, session_id: str) -> list[dict]:
        raise NotImplementedError

    def reply_permission(self, session_id: str, request_id: str,
                         decision: str) -> bool:
        raise NotImplementedError

    def cancel(self, run: BackendRun) -> CancelResult:
        raise NotImplementedError


class _LiveStream:
    """One long-lived ``opencode run`` child, streamed incrementally
    ACROSS wait slices (M4-A3 live finding; the P4 shape called
    capture_run(timeout=deadline_s) once per slice, which (a) raised
    TypeError from capture_and_normalize's duplicate on_line and (b) at
    WAIT_POLL_S=0.1 would kill our own child on the first slice and
    misjudge every live job. The stream keeps ONE child per attempt and
    never re-sends the prompt; non-blocking select+os.read drains mirror
    capture_run's per-line framing/dedupe/judge semantics."""

    def __init__(self, binary: str, *, cwd: str, model: str, prompt: str,
                 attempt_id: str, title: str = "hina-run"):
        import io
        import selectors
        from .events import EventNormalizer
        from .opencode_v2 import (IncrementalLineDecoder, build_run_argv,
                                  build_run_env, parse_jsonl_line, utcnow)
        self._parse = parse_jsonl_line
        self._utcnow = utcnow
        self._decode = IncrementalLineDecoder.decode
        argv = build_run_argv(binary, model=model, title=title, prompt=prompt)
        self.proc = subprocess.Popen(argv, cwd=cwd, env=build_run_env(
            cwd, binary), shell=False, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ, "stdout")
        self.sel.register(self.proc.stderr, selectors.EVENT_READ, "stderr")
        self.decoder = IncrementalLineDecoder()
        self.norm = EventNormalizer(attempt_id)
        self.events: list = []
        self.out = io.BytesIO()
        self.err = io.BytesIO()
        self.eof = {"stdout": False, "stderr": False}
        self.mismatch = None
        self.finished = False
        self.exit_code: int | None = None
        self.done = False
        self.outcome: AttemptOutcome | None = None

    def _drain(self, name: str, sink) -> None:
        stream = (self.proc.stdout if name == "stdout"
                  else self.proc.stderr)
        chunk = os.read(stream.fileno(), 1 << 16)
        if not chunk:
            self.eof[name] = True
            try:
                self.sel.unregister(stream)
            except (KeyError, ValueError):
                pass
            return
        sink.write(chunk)
        if name == "stdout" and self.mismatch is None:
            try:
                for line in self.decoder.feed(chunk):
                    self._decode(line)   # UTF-8 validity always checked
                    self._record(line)
            except ContractMismatch as e:
                self.mismatch = e

    def _record(self, line: bytes) -> None:
        """Same framing/dedupe/normalize policy as
        capture_and_normalize.on_line (contract §3)."""
        env = self._parse(line)
        is_obj = isinstance(env, dict) and not env.get("parse_error")
        dup = self.norm.check_dup(env) if is_obj else None
        if dup is not None:
            self.norm.note_duplicate(dup)
            if dup.duplicate:   # same ID + identical payload -> drop
                return
        ev = self.norm.normalize(env, self._utcnow())
        if dup is not None and dup.conflict:
            ev.payload["dup_conflict"] = True
        self.events.append(ev)

    def pump(self, slice_s: float) -> None:
        """Non-blocking drain for up to one wait slice; NEVER kills the
        child."""
        deadline = time.monotonic() + max(0.0, slice_s)
        while not self.finished:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = self.sel.select(timeout=min(remaining, 0.2))
            except (OSError, ValueError):
                break
            for key, _ in ready:
                try:
                    self._drain(key.data,
                                self.out if key.data == "stdout" else self.err)
                except OSError:
                    self.eof[key.data] = True
            if self.proc.poll() is not None and not self.sel.get_map():
                self.finished = True
                self.exit_code = self.proc.returncode
        if not self.finished and self.proc.poll() is not None \
                and all(self.eof.values()):
            self.finished = True
            self.exit_code = self.proc.returncode

    def terminate_child(self) -> None:
        """Stop watching because the supervisor is cancelling: terminate
        ONLY our owned child (never a shared server). Session-side stop
        confirmation stays backend.cancel's job."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        try:
            self.sel.close()
        except Exception:
            pass

    def finalize(self, session_id: str) -> AttemptOutcome:
        from .events import judge_completion
        verdict = judge_completion(self.events, exit_code=self.exit_code,
                                   eof=True,
                                   contract_mismatch=self.mismatch is not None,
                                   killed_by_us=False)
        self.outcome = AttemptOutcome(verdict.verdict, verdict.reason,
                                      usage=verdict.usage,
                                      session_id=session_id,
                                      raw_stdout=self.out.getvalue(),
                                      exit_code=self.exit_code,
                                      answer_text=_final_answer_text(
                                          self.events))
        self.done = True
        try:
            self.sel.close()
        except Exception:
            pass
        return self.outcome


class LiveBackend(SupervisorBackend):
    """Real OpenCode V2 wiring (adapter P2). The wait path streams one
    long-lived child per attempt across slices (_LiveStream; M4-A3 live
    fix — offline tests now cover the slice streaming with a fake CLI).
    Mac end-to-end remains documented in PI/A3 evidence."""

    def __init__(self, *, binary, server_url, adapter, expected_version=None,
                 expected_binary_sha256=None, process_runner=None):
        from .opencode_v2 import OpenCodeV2Adapter, probe as oc_probe
        self.binary = binary
        self.server_url = server_url
        self._probe = oc_probe
        self.adapter = adapter if adapter is not None else \
            OpenCodeV2Adapter(__import__(
                "hina_opencode_worker.opencode_v2", fromlist=["x"])
                .SessionApiTransport(binary, server_url,
                                     process_runner=process_runner))
        self.expected_version = expected_version
        self.expected_binary_sha256 = expected_binary_sha256
        self._runs: dict[str, BackendRun] = {}
        self._streams: dict[str, _LiveStream] = {}

    def probe(self) -> dict:
        res = self._probe(self.binary, version_out=self.expected_version,
                          expected_sha256=self.expected_binary_sha256) \
            if self.expected_binary_sha256 else \
            self._probe(self.binary, version_out=self.expected_version)
        return {"version": res.version, "binary_sha256": res.sha256,
                "mapping_revision": self.adapter.transport.server_url and None
                or getattr(self, "mapping_revision", None)}

    def available(self, model: str) -> bool:
        return True  # catalog policy only; live availability is PI scope

    def start(self, *, job_id, attempt_id, model, cwd, prompt) -> BackendRun:
        provider_id, _, model_id = model.partition("/")
        session_id = self.adapter.create_session(
            title=f"hina-{job_id[:12]}",
            model={"providerID": provider_id, "id": model_id})
        run = BackendRun(session_id)
        self._runs[session_id] = run
        run.handle = (cwd, model, prompt, attempt_id)
        return run

    def wait(self, run, *, cancel, deadline_s, on_permission):
        st = self._streams.get(run.session_id)
        if st is None:
            cwd, model, prompt, attempt_id = run.handle
            st = _LiveStream(self.binary, cwd=cwd, model=model,
                             prompt=prompt, attempt_id=attempt_id)
            self._streams[run.session_id] = st
        if st.done:
            return st.outcome
        if cancel.is_set():
            # supervisor's cancel branch owns the outcome; we stop
            # observing and terminate ONLY our child
            st.terminate_child()
            self._streams.pop(run.session_id, None)
            return None
        st.pump(deadline_s)
        if not st.finished:
            return None            # slice elapsed; child stays alive,
            # prompt NEVER re-sent
        outcome = st.finalize(run.session_id)
        self._streams.pop(run.session_id, None)
        return outcome

    def reconcile(self, session_id, timeout_s):
        return self.adapter.reconcile(session_id)

    def prompt(self, session_id, text, delivery):
        return self.adapter.prompt(session_id, text, delivery=delivery)

    def pending_permissions(self, session_id):
        return self.adapter.pending_permissions(session_id)

    def reply_permission(self, session_id, request_id, decision):
        return self.adapter.reply_permission(
            session_id, request_id, decision).ok

    def cancel(self, run):
        ack = self.adapter.interrupt(run.session_id)
        return self.adapter.confirm_cancel(run.session_id, ack=ack)


# ---------------------------------------------------------------------------
# cursors — MCP.md §5
# ---------------------------------------------------------------------------

class CursorBox:
    """base64url(payload) + HMAC-SHA256. Key file is 0600 and survives
    restarts (never regenerated while present)."""

    def __init__(self, key: bytes, time_fn=time.time):
        self._key = key
        self._time = time_fn

    def make(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True,
                       separators=(",", ":")).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(hmac.new(
            self._key, body.encode(), hashlib.sha256).digest()[:16]
        ).decode().rstrip("=")
        return f"v1.{body}.{sig}"

    def open(self, cursor: str, *, tool: str) -> dict:
        try:
            ver, body, sig = cursor.split(".")
            if ver != "v1":
                raise ValueError("version")
            want = base64.urlsafe_b64encode(hmac.new(
                self._key, body.encode(), hashlib.sha256).digest()[:16]
            ).decode().rstrip("=")
            if not hmac.compare_digest(want, sig):
                raise WorkerError("CURSOR_INVALID", "cursor signature invalid")
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except WorkerError:
            raise
        except Exception:
            raise WorkerError("CURSOR_INVALID", "cursor malformed")
        if payload.get("tool") != tool or payload.get("principal") is None:
            raise WorkerError("CURSOR_INVALID", "cursor bound to a different"
                                              " context")
        if payload.get("expires_at", 0) < self._time():
            raise WorkerError("CURSOR_EXPIRED",
                              "cursor past its 24h TTL — refetch first page")
        return payload


def _load_or_make_key(path: str) -> bytes:
    if os.path.exists(path):
        key = open(path, "rb").read()
        if len(key) < 16:
            raise WorkerError("INTERNAL", "key file corrupt")
        return key
    key = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _sanitize(text: str) -> str:
    text, _ = art.redact_text(text or "")
    return text[:2000]


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------

class SupervisorConfig:
    def __init__(self, *, root: str, gate="PASS",
                 profiles=("inspect", "develop"),
                 develop_enforcement_verified=False,
                 approval_channel_verified=False,
                 wait_poll_s=WAIT_POLL_S, dispatch_enabled=True,
                 pool_root=None,
                 operate_profile_path=None, operate_journal_dir=None,
                 operate_attempt_ttl_s=OPERATE_ATTEMPT_TTL_S):
        self.root = os.path.abspath(root)
        self.gate = gate
        self.profiles = list(profiles)
        self.develop_enforcement_verified = develop_enforcement_verified
        self.approval_channel_verified = approval_channel_verified
        self.wait_poll_s = wait_poll_s
        self.dispatch_enabled = dispatch_enabled
        self.db_path = os.path.join(self.root, "db", "worker.db")
        self.run_dir = os.path.join(self.root, "run")
        self.keys_dir = os.path.join(self.root, "keys")
        self.artifacts_dir = os.path.join(self.root, "artifacts")
        self.pool_root = pool_root or os.path.join(self.root, "pool")
        self.sock_path = os.path.join(self.run_dir, "supervisor.sock")
        self.lock_path = os.path.join(self.run_dir, "supervisor.lock")
        # Q3/ch7: operate is a SEPARATE profile + approval + recovery
        # plan; absent file means the capability is not installed.
        self.operate_profile_path = operate_profile_path or os.path.join(
            self.root, "operate.yaml")
        self.operate_journal_dir = operate_journal_dir or os.path.join(
            self.root, "operate")
        self.operate_attempt_ttl_s = operate_attempt_ttl_s


class Supervisor:
    """One supervisor per root (OS lock). Owns the current task; every
    tool returns a contract envelope dict."""

    VERSION = CONTRACT_VERSION

    def __init__(self, config: SupervisorConfig, *, backend, catalog,
                 peer_credentials=None, clock=time.monotonic,
                 time_fn=time.time, logger=None):
        self.config = config
        self.backend = backend
        self.catalog = catalog
        self._peer_credentials = peer_credentials  # sock -> (uid, gid) | None
        self._clock = clock
        self._time = time_fn
        self._log = logger or _NullLogger()
        for d in (config.root, config.run_dir, config.keys_dir,
                  config.artifacts_dir, config.pool_root,
                  os.path.dirname(config.db_path)):
            os.makedirs(d, mode=0o700, exist_ok=True)
        self._cursor_box = CursorBox(_load_or_make_key(
            os.path.join(config.keys_dir, "cursor.key")), time_fn=time_fn)
        self._approval_key = _load_or_make_key(
            os.path.join(config.keys_dir, "approval.key"))
        self._approval_journal = ApprovalJournal()
        self._operate_gate = opmod.OperateGate(
            profile_path=config.operate_profile_path,
            journal_dir=config.operate_journal_dir,
            time_fn=time_fn,
            attempt_ttl_s=config.operate_attempt_ttl_s,
            attempt_is_live=self._operate_attempt_is_live)
        self._lock = SupervisorLock(config.lock_path)
        self._lock.acquire()
        self.store = self._open_threaded_store(config.db_path)
        self.journal = RequestJournal(self.store)
        self.sm = StateMachine()
        self._uid = os.getuid()
        # P4 writer discipline: the single Store connection may only be
        # touched under this (reentrant) lock — no interleaved
        # BEGIN/COMMIT between threads. NEVER held across sleeps or
        # backend I/O (MCP.md §4); reads on worker_wait paths use the
        # separate _read() connection instead.
        self._db_write = threading.RLock()
        self._artifact_store = art.ArtifactStore(
            config.artifacts_dir, sink=art.SqliteRegistrySink(self.store.conn),
            clock=time_fn)
        self._current_job_id: str | None = None
        self._runners: dict[str, threading.Thread] = {}
        self._sig = threading.Lock()
        self._uds = None
        self._uds_thread = None
        self._closed = False
        plans = self.store.reconcile_after_restart()
        for _rid, plan in plans:
            if plan == "resume":
                self._log.note(f"resuming prepared request {_rid}")
                self._resume_prepared(_rid)

    # ------------------------------------------------------------- plumbing

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._uds is not None:
            try:
                self._uds.shutdown()
                self._uds.server_close()
            except Exception:
                pass
            self._uds = None
        try:
            self.store.close()
        finally:
            self._lock.release()

    @staticmethod
    def _open_threaded_store(path: str) -> Store:
        """Store.open() pins one thread; the supervisor's runner/handler
        threads share the writer under _db_write (short BEGIN IMMEDIATE
        only, never across sleeps). Same pragmas as P1 Store.open; P1 is
        not modified — P4 opens its connection itself. Documented in the
        P4 HANDOFF as the integration seam P3's durable_note awaits."""
        conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.close()
            raise RuntimeError(f"could not enable WAL (got {mode!r})")
        conn.execute("PRAGMA synchronous = FULL")
        store = Store(conn, path)
        store._migrate()
        return store

    def _read(self) -> sqlite3.Connection:
        """Per-call read-only connection (WAL readers never block the
        writer; wait() holds nothing)."""
        conn = sqlite3.connect(f"file:{self.store.path}?mode=ro", uri=True,
                               timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _db(self):
        """Short write/transaction region on the shared Store conn."""
        return self._db_write

    def _job_row(self, job_id):
        conn = self._read()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise NotFoundError(f"job {job_id} gone")
        return row

    def _job_version(self, job_id) -> int:
        conn = self._read()
        try:
            row = conn.execute("SELECT version FROM jobs WHERE job_id = ?",
                               (job_id,)).fetchone()
        finally:
            conn.close()
        return row["version"] if row is not None else -1

    def _latest_attempt_read(self, job_id):
        conn = self._read()
        try:
            return conn.execute(
                "SELECT * FROM attempts WHERE job_id = ?"
                " ORDER BY ordinal DESC LIMIT 1", (job_id,)).fetchone()
        finally:
            conn.close()

    def _audit_transport(self) -> None:
        st = os.stat(self.config.run_dir)
        if st.st_mode & 0o777 != 0o700:
            raise WorkerError("INTERNAL", "run dir mode must be 0700")
        if os.path.exists(self.config.sock_path):
            mode = os.stat(self.config.sock_path).st_mode & 0o777
            if mode != 0o600:
                raise WorkerError("FORBIDDEN",
                                  "supervisor socket mode must be 0600")

    # ------------------------------------------------------- UDS transport

    def start_uds(self) -> str:
        self._audit_transport()
        path = self.config.sock_path
        if os.path.exists(path):
            # stale socket from a dead supervisor only if our lock is free —
            # and it cannot be, we hold it; refuse to unlink an unknown.
            raise WorkerError("INTERNAL", "socket already bound")
        sup = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                sup._handle_conn(self.connection)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            allow_reuse_address = False

        server = Server(path, Handler)
        os.chmod(path, 0o600)
        self._uds = server
        self._uds_thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._uds_thread.start()
        return path

    def _peer_uid(self, conn: socket.socket):
        if self._peer_credentials is not None:
            return self._peer_credentials(conn)
        if hasattr(conn, "getpeereid"):
            try:
                uid, _gid = conn.getpeereid()
                return uid
            except OSError:
                return None
        # This host's CPython build has no getpeereid; macOS exposes the
        # same kernel-trusted peer identity via getsockopt(LOCAL_PEERCRED)
        # (struct xucred). Documented as a host deviation in the HANDOFF.
        return self._peer_uid_local(conn)

    @staticmethod
    def _peer_uid_local(conn: socket.socket):
        fmt = ("<" if sys.byteorder == "little" else ">") + "19I"
        try:
            raw = conn.getsockopt(getattr(socket, "SOL_LOCAL", 0),
                                  socket.LOCAL_PEERCRED,
                                  struct.calcsize(fmt))
        except OSError:
            return None
        if len(raw) < 12 or struct.unpack_from("I", raw)[0] != 0:
            return None
        return struct.unpack_from("I", raw, 4)[0]

    def _handle_conn(self, conn: socket.socket) -> None:
        """One request per connection: read ≤256KiB frame, answer, close.
        Peer UID is re-checked every connection (MCP.md §1)."""
        try:
            raw = b""
            hdr = self._recv_exact(conn, 4)
            if hdr is None:
                return
            (length,) = struct.unpack(">I", hdr)
            if length > MAX_INPUT_BYTES:
                resp = self._error_envelope(None, "INVALID_ARGUMENT",
                                            "input envelope exceeds 256KiB")
                self._send(conn, resp)
                return
            raw = self._recv_exact(conn, length)
            if raw is None:
                return
            try:
                req = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(conn, self._error_envelope(
                    None, "INVALID_ARGUMENT", "request is not UTF-8 JSON"))
                return
            uid = self._peer_uid(conn)
            peer = self._principal_from_peer(uid)
            if isinstance(peer, WorkerError):
                self._send(conn, self._error_envelope(
                    req.get("tool") if isinstance(req, dict) else None,
                    peer.code, peer.message))
                return
            self._send(conn, self.dispatch(req, principal=peer))
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _send(conn, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_RESPONSE_BYTES:
            obj = {"contract_version": CONTRACT_VERSION, "tool": None,
                   "observed_at": utc_now(),
                   "receipt": {"request_id": None, "replayed": False},
                   "error": {"code": "INTERNAL",
                             "message": "response exceeds 256KiB",
                             "retryable": False, "details": {}}}
            data = json.dumps(obj).encode("utf-8")
        conn.sendall(struct.pack(">I", len(data)) + data)

    def _principal_from_peer(self, uid):
        if uid is None:
            return WorkerError("UNAUTHENTICATED",
                               "peer credentials unavailable on this"
                               " connection")
        if uid != self._uid:
            return WorkerError("FORBIDDEN",
                               "peer uid is not the supervisor uid")
        return f"local:{uid}"

    def dispatch(self, req, *, principal: str) -> dict:
        """UDS request envelope: {client, tool|method, arguments}.
        client 'mcp_proxy' may call the 8 tools; client 'viewer' is
        read-only (snapshot/tail/requests) and cannot mutate
        anything."""
        if not isinstance(req, dict):
            return self._error_envelope(None, "INVALID_ARGUMENT",
                                        "request must be an object")
        client = req.get("client")
        tool = req.get("tool") or req.get("method")
        args = req.get("arguments") or {}
        try:
            self._audit_transport()
        except WorkerError as e:
            return self._error_envelope(tool, e.code, e.message)
        try:
            if client == "viewer":
                if tool not in ("viewer.snapshot", "viewer.tail",
                                "viewer.requests"):
                    return self._error_envelope(
                        tool, "FORBIDDEN",
                        "viewer connections are read-only")
                if tool == "viewer.snapshot":
                    return {"contract_version": CONTRACT_VERSION,
                            "method": tool, "data": self._viewer_snapshot()}
                if tool == "viewer.requests":
                    return {"contract_version": CONTRACT_VERSION,
                            "method": tool,
                            "data": self._viewer_requests(principal)}
                return {"contract_version": CONTRACT_VERSION,
                        "method": tool,
                        "data": self._viewer_tail(int(
                            args.get("after_seq", 0) or 0))}
            if client == "operator":
                # Q3/ch7: the operate surface is NOT one of the 8 MCP
                # tools; only an explicitly local operator client can
                # reach it, and only the dry-run plan paths exist.
                if tool not in OPERATE_METHODS:
                    return self._error_envelope(
                        tool, "FORBIDDEN",
                        "operator connections may only call "
                        "operate.plan/status/revalidate")
                return self._handle_operate(tool, args, principal)
            if client != "mcp_proxy":
                return self._error_envelope(tool, "UNAUTHENTICATED",
                                            "unknown client kind")
            return self.handle(tool, args, principal=principal)
        except WorkerError as e:
            return self._error_envelope(tool, e.code, e.message,
                                        details=e.details)
        except Exception:  # pragma: no cover — safety net
            self._log.exception("supervisor internal failure")
            return self._error_envelope(tool, "INTERNAL",
                                        "internal supervisor failure")

    def handle(self, tool, args, *, principal: str) -> dict:
        if tool not in TOOL_VALIDATORS:
            return self._error_envelope(tool, "INVALID_ARGUMENT",
                                        f"unknown tool {tool!r}")
        if not principal.startswith("local:") or not \
                principal.split(":")[-1].isdigit() or \
                int(principal.split(":")[-1]) != self._uid:
            return self._error_envelope(tool, "UNAUTHENTICATED",
                                        "principal is not this host's"
                                        " local principal")
        try:
            validated = TOOL_VALIDATORS[tool](args
                                              if isinstance(args, dict)
                                              else {})
            fn = getattr(self, "_tool_" + tool.removeprefix("worker_"))
            return fn(validated, principal)
        except WorkerError as e:
            return self._error_envelope(tool, e.code, _sanitize(e.message),
                                        details=e.details)

    # ------------------------------------------------------------ envelopes

    def _envelope(self, tool, *, request_id=None, replayed=False,
                  data=None, error=None) -> dict:
        env = {"contract_version": CONTRACT_VERSION, "tool": tool,
               "observed_at": utc_now(),
               "receipt": {"request_id": request_id,
                           "replayed": bool(replayed)}}
        if error is not None:
            env["error"] = error
        else:
            env["data"] = data
        return env

    def _error_envelope(self, tool, code, message, *, request_id=None,
                        details=None, replayed=False) -> dict:
        assert code in ERROR_CODES
        safe = {}
        for k, v in (details or {}).items():
            safe[str(k)[:64]] = _sanitize(str(v))[:1024]
            if len(safe) >= 16:
                break
        return self._envelope(tool, request_id=request_id, replayed=replayed,
                              error={"code": code,
                                     "message": _sanitize(message)[:2000]
                                     or code,
                                     "retryable": code in RETRYABLE,
                                     "details": safe})

    # ------------------------------------------------------------- job views

    def _latest_attempt(self, conn, job_id):
        return conn.execute(
            "SELECT * FROM attempts WHERE job_id = ?"
            " ORDER BY ordinal DESC LIMIT 1", (job_id,)).fetchone()

    def _pending_approvals(self, conn, job, attempt_id) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE job_id = ? AND status = 'pending'"
            " ORDER BY created_at", (job["job_id"],)).fetchall()
        out = []
        now_epoch = self._time()
        for r in rows:
            try:
                exp_epoch = datetime.strptime(
                    r["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                exp_epoch = 0
            if exp_epoch < now_epoch:
                continue
            challenge = None
            if self.config.approval_channel_verified:
                chal_grant = ApprovalGrant(
                    approval_id=r["approval_id"], attempt_id=r["attempt_id"],
                    backend_request_id=r["backend_request_id"],
                    principal=r["principal_id"],
                    scope_sha256=r["scope_sha256"],
                    generation=r["generation"], expires_at=exp_epoch,
                    channel_verified=True, decision="once", challenge=None)
                challenge = compute_challenge(self._approval_key, chal_grant)
            try:
                summary = json.dumps(json.loads(r["scope_json"]),
                                     ensure_ascii=False)[:4096]
            except Exception:
                summary = "{}"
            out.append({
                "approval_id": r["approval_id"], "attempt_id": r["attempt_id"],
                "action": r["action"], "scope_summary": summary,
                "scope_sha256": r["scope_sha256"],
                "challenge": challenge,
                "expires_at": r["expires_at"],
                "channel_status": "verified"
                if self.config.approval_channel_verified else "unavailable"})
        return out

    def _job_view_conn(self, conn, job_id: str, *, principal: str) -> dict:
        job = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise WorkerError("NOT_FOUND", "job not found")
        return self._job_row_view(conn, job)

    def _job_row_view(self, conn, job) -> dict:
        att = self._latest_attempt(conn, job["job_id"])
        return {
            "job_id": job["job_id"],
            "active_attempt_id": job["active_attempt_id"],
            "state": job["state"],
            "verification_status": job["verification_status"],
            "acceptance": job["acceptance"],
            "model": att["model"] if att is not None
            else job["requested_model"],
            "workspace": att["workspace_path"] if att is not None else None,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "result_revision": job["result_revision"],
            "attention_reason": job["attention_reason"],
            "pending_approvals": self._pending_approvals(
                conn, job, job["active_attempt_id"]),
        }

    def _snapshot_id(self, conn, principal: str) -> str:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM state_events").fetchone()[0]
        return hashlib.sha256(
            f"{principal}|{count}|{seq}".encode()).hexdigest()[:32]

    # ----------------------------------------------------------- tool: catalog

    def _tool_catalog(self, args, principal):
        try:
            if args["refresh"]:
                eng = self.backend.probe()
                self.catalog.engine = eng
            else:
                eng = self.catalog.engine
        except ContractMismatch as e:
            return self._error_envelope("worker_catalog", "CONTRACT_MISMATCH",
                                        str(e))
        except (BackendUnavailable, ConnectionError, OSError) as e:
            return self._error_envelope("worker_catalog",
                                        "BACKEND_UNAVAILABLE",
                                        "engine probe unreachable")
        except Exception:
            return self._error_envelope("worker_catalog", "INTERNAL",
                                        "engine probe failed")
        caps = dict(self.catalog.capabilities)
        caps.setdefault("launch", True)
        caps.setdefault("interrupt", True)
        caps["approval_channel"] = self.config.approval_channel_verified
        caps["develop_enforcement"] = \
            self.config.develop_enforcement_verified
        data = {"engine": {"version": eng.get("version") or "unprobed",
                           "binary_sha256": eng.get("binary_sha256"),
                           "mapping_revision": eng.get("mapping_revision")},
                "models": self.catalog.rows(),
                "profiles": self.catalog.profiles,
                "capabilities": caps,
                "gate": self.catalog.gate}
        return self._envelope("worker_catalog", data=data)

    # -------------------------------------------------------------- journaling

    def _request_intake_simple(self, principal, request_id, tool, payload,
                               *, job_id=None, attempt_id=None,
                               pre_accept_check=None):
        """journal row create/replay for worker_cancel/worker_approve
        (P1 journal covers start/followup only; same conflict rules).
        Replay/conflict is resolved BEFORE any state validation so an
        accepted request is never refused as stale/busy (STORAGE.md/T02
        ordering); the pre-check runs only for a new request, inside the
        same tx as the insert."""
        with self._db_write, self.store.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM requests WHERE principal_id=? AND"
                " request_id=?", (principal, request_id)).fetchone()
            sha = payload_sha256(canonical_json(payload))
            if existing is not None:
                if existing["tool"] != tool \
                        or existing["payload_sha256"] != sha:
                    raise WorkerError(
                        "IDEMPOTENCY_CONFLICT",
                        "request_id already accepted with a different"
                        " payload")
                return existing, True
            if pre_accept_check is not None:
                pre_accept_check(conn)
            now = utc_now()
            try:
                self.store.insert_request(
                    conn, principal_id=principal, request_id=request_id,
                    tool=tool, payload_sha256=sha,
                    normalized_json=canonical_json(payload),
                    job_id=job_id, attempt_id=attempt_id, phase="accepted",
                    receipt={"request_id": request_id, "job_id": job_id,
                             "attempt_id": attempt_id, "phase": "accepted",
                             "payload_sha256": sha, "created_at": now},
                    created_at=now, updated_at=now)
            except sqlite3.IntegrityError:
                raise WorkerError(
                    "IDEMPOTENCY_CONFLICT",
                    "request_id collided with a concurrent request")
            except StorageFullError:
                raise WorkerError("STORAGE_FULL", "storage full")
            except DatabaseLockedError:
                raise WorkerError("INTERNAL", "database locked")
        row = self._request_row(principal, request_id)
        return row, False

    def _request_row(self, principal, request_id):
        conn = self._read()
        try:
            return conn.execute(
                "SELECT * FROM requests WHERE principal_id = ? AND"
                " request_id = ?", (principal, request_id)).fetchone()
        finally:
            conn.close()

    def _request_complete(self, principal, request_id) -> None:
        row = self._request_row(principal, request_id)
        if row is None or row["phase"] == "completed":
            return
        if row["phase"] == "accepted":
            # cancel/approve requests never enter the dispatch machine
            # (P1's journal graph owns dispatch phases only); the schema
            # CHECK allows accepted -> completed directly.
            with self._db(), self.store.tx() as conn:
                conn.execute(
                    "UPDATE requests SET phase = 'completed', updated_at = ?"
                    " WHERE principal_id = ? AND request_id = ?"
                    " AND phase = 'accepted'",
                    (utc_now(), principal, request_id))
            return
        with self._db():
            try:
                self.journal.mark_completed(principal, request_id)
            except Exception:
                pass

    def _request_finish(self, principal, request_id, job_state) -> None:
        """Dispatch lifecycle convergence so a NORMAL terminal never
        leaves a request in sending/acknowledged (which a later restart
        would misread as an ambiguous crash window)."""
        row = self._request_row(principal, request_id)
        if row is None or row["phase"] in ("completed", "failed",
                                           "ambiguous"):
            return
        if job_state in ("completed", "cancelled"):
            to = "completed"
        elif job_state == "failed":
            to = "failed"
        else:
            return  # needs_attention etc. keep the crash-window semantics
        with self._db():
            try:
                if row["phase"] == "sending":
                    self.journal.mark_acknowledged(principal, request_id)
                if to == "completed":
                    self.journal.mark_completed(principal, request_id)
                else:
                    self.journal.mark_failed(principal, request_id)
            except Exception:
                pass

    # -------------------------------------------------------------- busy gate

    def _busy_error(self):
        return WorkerError(
            "BUSY", "M1 allows exactly one current task; another job is"
                    " active and no auto-queue is created", details={"current_job": self._current_job_id or "see worker_status"})

    def _busy_check(self, principal, request_id):
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE principal_id = ?"
                " AND state IN (" + ",".join("?" * len(ACTIVE_JOB_STATES))
                + ") ORDER BY created_at DESC LIMIT 1",
                (principal, *sorted(ACTIVE_JOB_STATES))).fetchone()
        finally:
            conn.close()
        if row is not None:
            self._current_job_id = row["job_id"]
            raise self._busy_error()

    # ------------------------------------------------------------- tool: start

    def _tool_start(self, a, principal):
        v = a
        model = v["model"]
        self.catalog.check(model)
        if v["profile"] == "develop" and \
                not self.config.develop_enforcement_verified:
            raise WorkerError(
                "CAPABILITY_UNVERIFIED",
                "develop profile requires verified worktree enforcement"
                " (M0 scope); not enabled yet")
        repo_path = v["workspace"]["repository"]

        def pre_check(principal_id, request_id):
            self._busy_check(principal_id, request_id)
            self._validate_repo_at_start(v, repo_path)

        try:
            with self._db():
                receipt, replayed = self.journal.intake(
                    principal, v["request_id"], "worker_start",
                    self._start_journal_payload(v),
                    pre_accept_check=pre_check,
                    billing_class=self.catalog.billing_class(model),
                    binary_sha256=(self.catalog.engine.get("binary_sha256")
                                   or "0" * 64))
        except BusyError as e:
            raise WorkerError("BUSY", _sanitize(str(e)))
        except IdempotencyConflictError as e:
            raise WorkerError("IDEMPOTENCY_CONFLICT", _sanitize(str(e)))
        except InvalidRequestError as e:
            raise WorkerError("INVALID_ARGUMENT", _sanitize(str(e)))
        except StorageFullError:
            raise WorkerError("STORAGE_FULL", "storage full")
        except DatabaseLockedError:
            raise WorkerError("INTERNAL", "database locked; safe-fail")
        job_id = receipt["job_id"]
        if self.config.dispatch_enabled:
            # Arm on replays too: the runner's live-phase guard plus the
            # prepared->sending CAS keep dispatch exactly-once across a
            # restart (replaying a still-'prepared' request after a kill
            # resumes it), while a claimed/acknowledged/completed request
            # is a no-op — never a second prompt (M0-09).
            self._start_runner(principal, v["request_id"], job_id, v)
        conn = self._read()
        try:
            view = self._job_view_conn(conn, job_id, principal=principal)
        finally:
            conn.close()
        return self._envelope("worker_start", request_id=v["request_id"],
                              replayed=replayed, data={"job": view})

    def _start_journal_payload(self, v) -> dict:
        # The durable intent must be COMPLETE (N3 resume): context and
        # verification_commands belong to the frozen record so a resumed
        # dispatch runs exactly what the uninterrupted one would have.
        return {k: v[k] for k in
                ("user_request_ref", "goal", "workspace", "profile", "model",
                 "constraints", "done_when", "context",
                 "verification_commands")}

    def _validate_repo_at_start(self, v, repo_path) -> None:
        try:
            repo = ws.verify_repository(repo_path)
        except ws.WorkspaceError as e:
            raise WorkerError("INVALID_ARGUMENT",
                              f"repository refused: {_sanitize(str(e))}")
        try:
            base_sha = ws.resolve_ref(repo,
                                      v["workspace"].get("source_ref", "HEAD"))
        except ws.WorkspaceError as e:
            raise WorkerError("INVALID_ARGUMENT",
                              f"source_ref does not resolve: "
                              f"{_sanitize(str(e))}")
        v["base_sha"] = base_sha
        if not v["committed_only"]:
            entries = ws.porcelain_status(repo.path)
            if entries and not v["include_existing_changes"]:
                raise WorkerError(
                    "WORKSPACE_DIRTY",
                    "source workspace has changes; pass"
                    " include_existing_changes with explicit context_files"
                    " or use_committed_only", details={"dirty_entries": str(len(entries))})
            if entries and v["include_existing_changes"]:
                policy = ws.DirtyInputPolicy(
                    include_existing_changes=True,
                    context_files=tuple(v["context_files"]))
                try:
                    policy.validate()
                    ws.prepare_inputs(repo, base_sha, policy=policy)
                except ws.WorkspaceError as e:
                    raise WorkerError("INVALID_ARGUMENT",
                                      f"context_files refused: "
                                      f"{_sanitize(str(e))}")
        canonical = os.path.realpath(repo.path)
        conn = self._read()
        try:
            held = conn.execute(
                "SELECT job_id, lease_state FROM workspaces WHERE"
                " canonical_path = ? AND lease_state != 'released'",
                (canonical,)).fetchone()
        finally:
            conn.close()
        if held is not None:
            raise WorkerError(
                "WORKSPACE_CONFLICT",
                "workspace lease held by another job", details={"holder_job": held["job_id"]})

    # -------------------------------------------------------------- runner

    def _start_runner(self, principal, request_id, job_id, v) -> None:
        with self._sig:
            if job_id in self._runners:
                return
            t = threading.Thread(
                target=self._run_attempt,
                args=(principal, request_id, job_id, v), daemon=True)
            self._runners[job_id] = t
            t.start()

    # -------------------------------------------------------- crash recovery

    def _resume_prepared(self, request_id) -> None:
        """STORAGE.md §3 ④: 'prepared' is durable, never-dispatched intent
        and restart is the only legitimate resume point. Re-arm the
        journaled dispatch exactly once — the runner's phase guard plus the
        prepared->sending CAS stay the single dispatch gate, so a losing
        racer (e.g. a client replay arriving concurrently) is a no-op.
        Requests reconciled to ambiguous + needs_attention are never
        re-armed here: an unknown dispatch exits on proof only (M0-09)."""
        if not self.config.dispatch_enabled:
            self._log.note(f"prepared request {request_id} left for replay"
                           " (dispatch disabled)")
            return
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,)).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (row["job_id"],)).fetchone() \
                if row is not None and row["job_id"] else None
        finally:
            conn.close()
        if row is None or job is None or row["phase"] != "prepared":
            return
        try:
            payload = json.loads(row["normalized_json"])
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if row["tool"] == "worker_start":
            if job["state"] != "accepted":
                # The state machine says the dispatch already moved on;
                # re-arming would risk a second prompt.
                return
            self._start_runner(row["principal_id"], row["request_id"],
                               row["job_id"],
                               self._v_from_normalized(payload,
                                                       row["request_id"]))
        elif row["tool"] == "worker_followup":
            if job["state"] != "starting" or "mode" not in payload:
                # 'accepted'-phase delivery/cancel/approve rows never enter
                # the dispatch machine; leave them untouched.
                return
            self._start_followup_runner(
                row["principal_id"], row["request_id"], row["job_id"],
                row["attempt_id"], payload.get("mode"), payload)

    def _v_from_normalized(self, payload: dict, request_id: str) -> dict:
        """Rebuild the validated start intent from the journal's frozen
        normalized payload — the exact dispatch the kill interrupted.
        context/verification_commands are part of the durable record, so
        the resumed run matches what the uninterrupted dispatch would have
        executed (defaults resolved at first acceptance, never re-resolved)."""
        ws_in = payload.get("workspace")
        if not isinstance(ws_in, dict):
            ws_in = {}
        return {
            "request_id": request_id,
            "user_request_ref": payload.get("user_request_ref", ""),
            "goal": payload.get("goal", ""),
            "workspace": dict(ws_in),
            "include_existing_changes": bool(
                ws_in.get("include_existing_changes", False)),
            "context_files": list(ws_in.get("context_files", [])),
            "committed_only": bool(ws_in.get("use_committed_only", False)),
            "profile": payload.get("profile", "inspect"),
            "model": payload.get("model", ""),
            "constraints": list(payload.get("constraints", [])),
            "done_when": list(payload.get("done_when", [])),
            "context": payload.get("context", ""),
            "verification_commands": list(
                payload.get("verification_commands", [])),
        }

    def _cas(self, job_id, version, to_state, reason, **kw) -> int:
        with self._db_write:
            return self.store.transition_job(job_id, version, to_state,
                                             reason, **kw)

    def _run_attempt(self, principal, request_id, job_id, v) -> None:
        cancel_ev = threading.Event()
        self._cancel_events[job_id] = cancel_ev
        run_obj = None
        lease = None
        handle = None
        try:
            with self._db():
                cur = self.store.get_request(principal, request_id)
                phase = cur["phase"] if cur is not None else None
                if phase == "accepted":
                    self.journal.mark_prepared(principal, request_id)
                elif phase != "prepared":
                    # Crash re-entry: sending/acknowledged/ambiguous means
                    # the dispatch is claimed or reconciled (never
                    # auto-re-dispatch an unknown); completed/failed rows
                    # are terminal. Either way this runner does nothing.
                    return
                claimed = self.journal.claim_dispatch(principal, request_id)
            if not claimed:
                return  # someone else won the CAS; never double-dispatch
            if v.get("base_sha"):
                # persist the resolved baseline sha (source_ref string from
                # intake) so drift between acceptance and dispatch is
                # checkable and auditable (WORKSPACE_CHANGED).
                with self._db(), self.store.tx() as c:
                    c.execute(
                        "UPDATE jobs SET base_revision = ?, updated_at = ?"
                        " WHERE job_id = ?",
                        (v["base_sha"], utc_now(), job_id))
            job = self._job_row(job_id)
            att = self._latest_attempt_read(job_id)
            attempt_id = att["attempt_id"]
            with self._sig:
                self._current_job_id = job_id

            # baseline drift check (WORKSPACE_CHANGED)
            try:
                repo = ws.verify_repository(v["workspace"]["repository"])
                now_sha = ws.resolve_ref(
                    repo, v["workspace"].get("source_ref", "HEAD"))
            except ws.WorkspaceError as e:
                self._fail_job(job_id, job["version"], "WORKSPACE_CHANGED",
                               f"baseline refused: {_sanitize(str(e))}")
                return
            if now_sha != v.get("base_sha", now_sha):
                self._fail_job(job_id, job["version"], "WORKSPACE_CHANGED",
                               "source_ref moved between acceptance and"
                               " dispatch; a new start with the new"
                               " baseline is required")
                return
            if not v.get("base_sha"):
                # Crash-resumed start: a prepared request never reached the
                # baseline-persist step. Re-resolve at dispatch time and
                # persist the sha exactly like the uninterrupted dispatch
                # would have (auditable sha, never the bare ref string).
                v["base_sha"] = now_sha
                with self._db(), self.store.tx() as c:
                    c.execute(
                        "UPDATE jobs SET base_revision = ?, updated_at = ?"
                        " WHERE job_id = ?", (now_sha, utc_now(), job_id))

            # model re-check at dispatch (no auto-switch, ever): the
            # catalog AND the live backend must both still know the model
            model = v["model"]
            if not (self.catalog.available(model)
                    and self.backend.available(model)):
                self._enter_model_selection(job_id, job["version"],
                                            attempt_id, model)
                return

            self._cas(job_id, job["version"], "starting", "dispatch",
                      attempt_id=attempt_id)
            self._update_attempt_state(attempt_id, "starting")
            with self._db_write:
                self.journal.mark_acknowledged(principal, request_id)

            cwd = None
            if v["profile"] == "develop":
                cwd, lease, handle = self._prepare_worktree(
                    job_id, attempt_id, repo, v)

            prompt = build_prompt(v)
            try:
                run_obj = self.backend.start(job_id=job_id,
                                             attempt_id=attempt_id,
                                             model=model,
                                             cwd=cwd or repo.path,
                                             prompt=prompt)
            except ModelUnavailableError:
                self._enter_model_selection(job_id,
                                            self._job_row(job_id)
                                            ["version"], attempt_id, model)
                return
            except ContractMismatch as e:
                self._fail_job(job_id, self._job_version(job_id),
                               "CONTRACT_MISMATCH", _sanitize(str(e)))
                return
            except (BackendUnavailable, ConnectionError, OSError):
                self._fail_job(job_id, self._job_version(job_id),
                               "BACKEND_UNAVAILABLE",
                               "backend start failed; no prompt may be"
                               " re-sent without checking the session")
                return
            except BackendTimeout:
                self._needs_attention(job_id,
                                      self._job_version(job_id),
                                      "BACKEND_TIMEOUT: backend start"
                                      " outcome unclear", attempt_id)
                return
            with self._db_write:
                self.store.conn.execute(
                    "UPDATE attempts SET session_id = ?, started_at = ?"
                    " WHERE attempt_id = ?",
                    (run_obj.session_id, utc_now(), attempt_id))
            self._cas(job_id, self._job_version(job_id),
                      "running", "session_started", attempt_id=attempt_id)
            self._update_attempt_state(attempt_id, "running")

            outcome = self._await_run(job_id, attempt_id, run_obj,
                                      cancel_ev)
            if outcome is None:
                return  # cancel or model-selection path finalized it

            if outcome.verdict == VERDICT_COMPLETED:
                self._finish_completed(job_id, attempt_id, run_obj, v,
                                       outcome, handle)
            elif outcome.verdict in (VERDICT_CLI_FAILED,
                                     VERDICT_CLI_OK_TOOL_FAILED,
                                     VERDICT_EMPTY_OUTPUT,
                                     VERDICT_NO_FINAL_ANSWER,
                                     VERDICT_CONTRACT_MISMATCH):
                reason = {"CLI_FAILED": "BACKEND_TIMEOUT",
                          "CONTRACT_MISMATCH": "CONTRACT_MISMATCH"}.get(
                    outcome.verdict, "RUN_FAILED")
                self._finish_failed(job_id, attempt_id, outcome, reason)
            else:  # UNKNOWN — execution unclear
                self._needs_attention(job_id,
                                      self._job_version(job_id),
                                      f"backend stop/completion unclear:"
                                      f" {_sanitize(outcome.reason)}",
                                      attempt_id)
            self._request_complete(principal, request_id)
        except WorkerError as e:
            try:
                self._fail_job(job_id, self._job_version(job_id),
                               e.code, e.message)
            except Exception:
                pass
        except Exception:
            self._log.exception("runner failure")
            try:
                self._fail_job(job_id, self._job_version(job_id),
                               "INTERNAL", "runner internal failure")
            except Exception:
                pass
        finally:
            try:
                self._request_finish(principal, request_id,
                                     self._read_job_state(job_id))
            except Exception:
                pass
            with self._sig:
                self._runners.pop(job_id, None)
                if self._current_job_id == job_id:
                    self._current_job_id = None
            self._cancel_events.pop(job_id, None)

    # runner helpers ------------------------------------------------------

    def _prepare_worktree(self, job_id, attempt_id, repo, v):
        base_sha = v["base_sha"]
        canonical = os.path.realpath(repo.path)
        with self._db_write:
            token = self.store.acquire_workspace_lease(
                canonical_path=canonical, job_id=job_id,
                attempt_id=attempt_id, executor_id="local",
                git_common_dir=repo.common_dir, base_revision=base_sha,
                owner_pid=os.getpid())
        sink = ws.SqliteLeaseSink(self.store.conn)
        handle = ws.create_worktree(
            repo, base_sha, pool_root=self.config.pool_root,
            job_id=job_id, attempt_id=attempt_id, executor_id="local",
            sink=sink)
        if v["include_existing_changes"] and v["context_files"]:
            snapshot = ws.prepare_inputs(
                repo, base_sha, policy=ws.DirtyInputPolicy(
                    include_existing_changes=True,
                    context_files=tuple(v["context_files"])))
            ws.apply_inputs(handle, snapshot)
        return handle.canonical_path, (canonical, token), handle

    def _await_run(self, job_id, attempt_id, run_obj, cancel_ev):
        """Poll loop. NEVER holds a lock across sleeps; backend polls
        respect a bounded timeout (MCP.md §4). Returns an AttemptOutcome
        or None when a terminal branch already fired."""
        # host bound only; a worker_wait budget is a call-response budget,
        # not the job lifetime (R5), so the runner is not bounded by it.
        last_probe = 0.0
        while True:
            if cancel_ev.is_set():
                return self._execute_cancel(job_id, attempt_id, run_obj)
            job_state = self._read_job_state(job_id)
            if job_state in ("cancel_requested",):
                return self._execute_cancel(job_id, attempt_id, run_obj)
            if job_state == "waiting_approval":
                decision = self._await_approval(job_id, attempt_id)
                if decision == "cancel":
                    return self._execute_cancel(job_id, attempt_id, run_obj)
                if decision == "expired":
                    self._cas(job_id,
                              self._job_version(job_id),
                              "needs_attention", "attention",
                              attempt_id=attempt_id,
                              attention_reason="APPROVAL_EXPIRED: pending"
                                               " approval TTL passed;"
                                               " backend pending state"
                                               " must be re-checked")
                    return None
                # decided; worker_approve already moved the job back to
                # running — fall through to keep observing the run.
                if self._read_job_state(job_id) == "waiting_approval":
                    self._cas(job_id,
                              self._job_version(job_id),
                              "running", "approval_resolved",
                              attempt_id=attempt_id)
            outcome = self.backend.wait(
                run_obj, cancel=cancel_ev,
                deadline_s=self.config.wait_poll_s,
                on_permission=lambda pr: self._on_permission(
                    job_id, attempt_id, pr))
            if outcome is not None:
                if outcome.model_unavailable:
                    self._enter_model_selection(
                        job_id, self._job_version(job_id),
                        attempt_id, self._attempt_model(attempt_id))
                    return None
                return outcome
            if self._clock() - last_probe > MODEL_PROBE_POLL_S:
                last_probe = self._clock()
                model = self._attempt_model(attempt_id)
                if model and not (self.catalog.available(model)
                                  and self.backend.available(model)):
                    self._enter_model_selection(
                        job_id, self._job_version(job_id),
                        attempt_id, model)
                    return None

    def _attempt_model(self, attempt_id) -> str:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT model FROM attempts WHERE attempt_id = ?",
                (attempt_id,)).fetchone()
        finally:
            conn.close()
        return row["model"] if row else ""

    def _read_job_state(self, job_id) -> str:
        conn = self._read()
        try:
            row = conn.execute("SELECT state FROM jobs WHERE job_id = ?",
                               (job_id,)).fetchone()
            return row["state"] if row else "cancelled"
        finally:
            conn.close()

    def _update_attempt_state(self, attempt_id, to_state) -> None:
        with self._db_write:
            self.store.update_attempt_state(attempt_id, to_state)

    def _attempt_state(self, attempt_id) -> str:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?",
                (attempt_id,)).fetchone()
            return row["state"] if row else "running"
        finally:
            conn.close()

    def _attempt_close(self, attempt_id, to_state) -> None:
        """Close an attempt on the shared state graph (states.json):
        running must pass through collecting for completed/failed and
        through cancel_requested for cancelled. update_attempt_state is
        idempotent for frm == to_state."""
        frm = self._attempt_state(attempt_id)
        if frm == to_state or self.sm.is_terminal_for_attempt(frm):
            return          # attempts are never reused or re-closed
        if not self.sm.is_allowed(frm, to_state):
            mid = ("cancel_requested" if to_state == "cancelled"
                   else "collecting")
            if self.sm.is_allowed(frm, mid):
                self._update_attempt_state(attempt_id, mid)
        self._update_attempt_state(attempt_id, to_state)

    def _fail_job(self, job_id, version, reason, message) -> None:
        try:
            att = self._latest_attempt_read(job_id)
            self._cas(job_id, version, "failed", reason,
                      attempt_id=att["attempt_id"] if att else None)
            if att is not None:
                self._update_attempt_state(att["attempt_id"], "failed")
        except (CasConflictError, InvariantFailureError):
            job = self._job_row(job_id)
            att = self._latest_attempt_read(job_id)
            self._needs_attention(job_id, job["version"],
                                  f"{reason}: {_sanitize(message)}",
                                  att["attempt_id"] if att else None)

    def _needs_attention(self, job_id, version, reason, attempt_id) -> None:
        self._cas(job_id, version, "needs_attention", "attention",
                  attempt_id=attempt_id, attention_reason=_sanitize(reason))

    def _enter_model_selection(self, job_id, version, attempt_id, model) -> None:
        """T17: an unavailable model NEVER auto-switches — not free→free,
        not free→go. The attempt is closed as failed, the job waits for
        an explicit user decision."""
        job = self._job_row(job_id)
        version, frm = job["version"], job["state"]
        if frm == "accepted":
            # states.json has no accepted->waiting_model_selection edge:
            # dispatch was already attempted, so record it honestly.
            version = self._cas(job_id, version, "starting", "dispatch",
                                attempt_id=attempt_id)
        try:
            self._attempt_close(attempt_id, "failed")
        except (InvariantFailureError, NotFoundError):
            pass
        self._cas(job_id, version, "waiting_model_selection",
                  "model_unavailable",
                  attention_reason=f"model {model!r} unavailable; awaiting"
                                   " user select_model decision (no"
                                   " auto-switch)")

    def _execute_cancel(self, job_id, attempt_id, run_obj):
        res: CancelResult = self.backend.cancel(run_obj)
        job = self._job_row(job_id)
        if res.confirmed:
            self._attempt_close(attempt_id, "cancelled")
            self._cas(job_id, job["version"], "cancelled",
                      "cancel_confirmed", attempt_id=attempt_id)
            self._release_lease_for(job_id)
        else:
            self._needs_attention(
                job_id, job["version"],
                f"CANCEL_UNCONFIRMED: {_sanitize(res.reason)}"
                " (workspace lease retained)", attempt_id)
        return None

    def _release_lease_for(self, job_id) -> None:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT canonical_path, fencing_token FROM workspaces"
                " WHERE job_id = ? AND lease_state = 'held'",
                (job_id,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            try:
                with self._db_write:
                    self.store.release_workspace_lease(
                        row["canonical_path"], row["fencing_token"])
            except (NotFoundError, Exception):
                pass

    def _finish_completed(self, job_id, attempt_id, run_obj, v, outcome,
                          handle):
        job = self._job_row(job_id)
        self._cas(job_id, job["version"], "collecting", "verdict_completed",
                  attempt_id=attempt_id)
        checks = self._run_checks(job_id, attempt_id, v, handle)
        revision = 1
        evidence_ok = True
        try:
            self._write_result_artifacts(job_id, attempt_id, revision, v,
                                         outcome, handle, checks)
        except (WorkerError, art.ArtifactError):
            evidence_ok = False
        verification = "passed"
        if any(c["verdict"] == "failed" for c in checks):
            verification = "failed"
        elif checks and any(c["verdict"] == "unknown" for c in checks):
            verification = "partial"
        with self._db_write:
            self.store.conn.execute(
                "UPDATE jobs SET verification_status = ?, result_revision = ?,"
                " updated_at = ? WHERE job_id = ?",
                (verification, revision, utc_now(), job_id))
        if not evidence_ok:
            job = self._job_row(job_id)
            self._needs_attention(job_id, job["version"],
                                  "EVIDENCE_INCOMPLETE: result artifacts"
                                  " could not be persisted", attempt_id)
            return
        job = self._job_row(job_id)
        self._cas(job_id, job["version"], "completed", "result_committed",
                  attempt_id=attempt_id)
        self._attempt_close(attempt_id, "completed")
        self._release_lease_for(job_id)

    def _finish_failed(self, job_id, attempt_id, outcome, reason) -> None:
        job = self._job_row(job_id)
        self._attempt_close(attempt_id, "failed")
        self._cas(job_id, job["version"], "failed", reason,
                  attempt_id=attempt_id)
        self._release_lease_for(job_id)

    def _run_checks(self, job_id, attempt_id, v, handle) -> list[dict]:
        checks = []
        if v["profile"] != "develop" or not v["verification_commands"]:
            return checks
        cwd, env = ws.exec_plan(handle) if handle is not None \
            else (None, dict(os.environ))
        for vc in v["verification_commands"]:
            started = utc_now()
            try:
                p = subprocess.run(
                    vc["argv"], cwd=os.path.join(cwd, vc["cwd_relative"])
                    if cwd else None,
                    env=env, shell=False, capture_output=True, check=False,
                    timeout=vc["timeout_ms"] / 1000.0)
                exit_code, out = p.returncode, p.stdout + p.stderr
            except subprocess.TimeoutExpired:
                exit_code, out = None, b"timeout"
            except OSError:
                exit_code, out = None, b"exec failed"
            verdict = "passed" if exit_code == 0 else \
                ("failed" if exit_code is not None else "unknown")
            check_id = new_id("chk")
            with self._db_write:
                self.store.conn.execute(
                    "INSERT INTO checks (check_id, job_id, attempt_id,"
                    " command_json, cwd, started_at, ended_at, exit_code,"
                    " verdict, source_snapshot_sha256)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (check_id, job_id, attempt_id,
                     json.dumps(vc["argv"]), vc["cwd_relative"], started,
                     utc_now(), exit_code, verdict,
                     handle.baseline_sha if handle is not None else "0" * 64))
            checks.append({"check_id": check_id, "command": vc["argv"],
                           "cwd": vc["cwd_relative"], "exit_code": exit_code,
                           "verdict": verdict, "output": out})
        return checks

    def _write_result_artifacts(self, job_id, attempt_id, revision, v,
                                outcome, handle, checks) -> None:
        """Evidence order (P3 ch4): private bytes first, exports derive
        from stored private via export_masked — never from live files.
        Only masked exports are ever served through worker_result."""
        summary_text = _sanitize(outcome.reason or "run completed")
        summary_text = f"goal: {v['goal'][:400]}\nresult: {summary_text}"
        # The connector must see the ANSWER, not just the verdict line
        # (M4-A3 round-trip delivery fix): verbatim trailing message text
        # under an `answer:` block. Secret-pattern redaction still applies
        # at masked-export time (P3 ch4); no other rewriting.
        if outcome.answer_text:
            summary_text = f"{summary_text}\nanswer:\n{outcome.answer_text}"
        with self._db():
            raw = outcome.raw_stdout or b""
            priv_raw = self._artifact_store.save_private(
                job_id=job_id, attempt_id=attempt_id,
                result_revision=revision, section="raw",
                relative_path="stdout.raw", data=raw,
                media_type="application/x-ndjson", source_sha256=None)
            self._artifact_store.export_masked(
                private=priv_raw, job_id=job_id, attempt_id=attempt_id,
                result_revision=revision,
                relative_path="content/stdout.raw")
            self._export_text(job_id, attempt_id, revision,
                              "summary/summary.txt", summary_text)
            changes = {"changes": [], "snapshot_sha256": None}
            if handle is not None:
                changes = handle.collect()
            self._export_text(
                job_id, attempt_id, revision, "changes/changes.json",
                json.dumps(changes, ensure_ascii=False, sort_keys=True))
            for c in checks:
                if c.get("output"):
                    self._export_text(
                        job_id, attempt_id, revision,
                        f"checks/check-{c['check_id']}.txt",
                        c["output"].decode("utf-8", "replace"))

    def _export_text(self, job_id, attempt_id, revision, rel_path,
                     text: str) -> None:
        """rel_path starts with summary/|changes/|checks/|content/."""
        priv_rel = "src/" + rel_path
        priv = self._artifact_store.save_private(
            job_id=job_id, attempt_id=attempt_id, result_revision=revision,
            section="raw", relative_path=priv_rel,
            data=text.encode("utf-8"), media_type="text/plain",
            source_sha256=None)
        self._artifact_store.export_masked(
            private=priv, job_id=job_id, attempt_id=attempt_id,
            result_revision=revision, relative_path=rel_path)

    def _export_row(self, conn, job_id, revision, export_rel):
        return conn.execute(
            "SELECT * FROM artifacts WHERE job_id = ? AND result_revision"
            " = ? AND visibility = 'export' AND relative_path = ?",
            (job_id, revision, "export/" + export_rel)).fetchone()

    # ----------------------------------------------------------- permission

    def _on_permission(self, job_id, attempt_id, request: dict) -> bool:
        """Backend permission hook. Issues an approval row per §7 and
        parks the job in waiting_approval. Returns nothing meaningful to
        the backend; the reply is only sent through worker_approve."""
        approval_id = new_id("apr")
        scope = request.get("patterns") or request.get("scope") or {}
        scope_json = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        from .events import scope_sha256
        sha = scope_sha256(request)
        expires = _rfc3339(self._time() + APPROVAL_TTL_S)
        with self._db_write:
            self.store.conn.execute(
                "INSERT INTO approvals (approval_id, job_id, attempt_id,"
                " principal_id, backend_request_id, generation, session_id,"
                " action, scope_json, scope_sha256, challenge_sha256, status,"
                " created_at, expires_at) VALUES (?,?,?,?,?,1,?,?,?,?,?,"
                "'pending',?,?)",
                (approval_id, job_id, attempt_id, f"local:{self._uid}",
                 request.get("id", request.get("requestID", "")),
                 request.get("sessionID", ""),
                 request.get("action", request.get("title", "action"))[:256],
                 scope_json, sha, sha, utc_now(), expires))
        try:
            job = self._job_row(job_id)
            self._cas(job_id, job["version"], "waiting_approval",
                      "permission_requested", attempt_id=attempt_id)
        except (CasConflictError, InvariantFailureError):
            pass

    def _await_approval(self, job_id, attempt_id) -> str:
        while True:
            conn = self._read()
            try:
                rows = conn.execute(
                    "SELECT status, expires_at FROM approvals WHERE"
                    " job_id = ? AND attempt_id = ? ORDER BY created_at",
                    (job_id, attempt_id)).fetchall()
                job_state = conn.execute(
                    "SELECT state FROM jobs WHERE job_id = ?",
                    (job_id,)).fetchone()
            finally:
                conn.close()
            if job_state is None or job_state["state"] == "cancel_requested":
                return "cancel"
            statuses = {r["status"] for r in rows}
            if statuses & {"approved", "denied"}:
                return "decided"
            exps = []
            for r in rows:
                try:
                    exps.append(datetime.strptime(
                        r["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp())
                except ValueError:
                    exps.append(0)
            if exps and all(e < self._time() for e in exps):
                return "expired"
            time.sleep(APPROVAL_POLL_S)

    # ------------------------------------------------------------- tool: status

    def _tool_status(self, a, principal):
        conn = self._read()
        try:
            if "job_id" in a:
                view = self._job_view_conn(conn, a["job_id"],
                                           principal=principal)
                return self._envelope("worker_status", data={
                    "jobs": [view], "next_cursor": None,
                    "snapshot_id": self._snapshot_id(conn, principal)})
            where, params = "", []
            filters_hash = ""
            if a.get("states"):
                where = ("WHERE state IN ("
                         + ",".join("?" * len(a["states"])) + ")")
                params += a["states"]
                filters_hash = hashlib.sha256(
                    ",".join(sorted(a["states"])).encode()).hexdigest()[:16]
            snapshot_id = self._snapshot_id(conn, principal)
            offset = 0
            if a.get("cursor"):
                payload = self._cursor_box.open(a["cursor"],
                                                tool="worker_status")
                if payload.get("filters_hash", "") != filters_hash \
                        or payload.get("snapshot_id") != snapshot_id \
                        or payload.get("principal") != principal:
                    raise WorkerError(
                        "CURSOR_INVALID",
                        "cursor belongs to a different snapshot or filter")
                offset = int(payload.get("next_offset", 0))
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC,"
                " job_id DESC LIMIT ? OFFSET ?",
                (*params, a["limit"] + 1, offset)).fetchall()
            page = rows[:a["limit"]]
            next_cursor = None
            if len(rows) > a["limit"]:
                next_cursor = self._cursor_box.make({
                    "principal": principal, "tool": "worker_status",
                    "section": None, "job": None, "attempt": None,
                    "filters_hash": filters_hash,
                    "snapshot_id": snapshot_id,
                    "next_offset": offset + a["limit"],
                    "result_revision": None,
                    "expires_at": self._time() + CURSOR_TTL_S})
            jobs = [self._job_row_view(conn, r) for r in page]
            return self._envelope("worker_status", data={
                "jobs": jobs, "next_cursor": next_cursor,
                "snapshot_id": snapshot_id})
        finally:
            conn.close()

    # --------------------------------------------------------------- tool: wait

    def _tool_wait(self, a, principal):
        job_id = a["job_id"]
        wait_ms = a["wait_ms"]
        deadline = self._clock() + wait_ms / 1000.0

        def snap():
            conn = self._read()
            try:
                view = self._job_view_conn(conn, job_id, principal=principal)
            finally:
                conn.close()
            return view

        view = snap()
        sig = (view["state"], view["result_revision"],
               view["active_attempt_id"], view["attention_reason"])
        last_reconcile = 0.0
        while True:
            now = self._clock()
            if now >= deadline:
                break
            try:
                view = snap()
            except WorkerError as e:
                if e.code == "NOT_FOUND":
                    raise
                raise WorkerError("INTERNAL", "wait read failed")
            cur_sig = (view["state"], view["result_revision"],
                       view["active_attempt_id"], view["attention_reason"])
            if cur_sig != sig:
                return self._envelope("worker_wait",
                                      data={"job": view,
                                            "wait_expired": False})
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            if view["state"] == "running" \
                    and now - last_reconcile >= MODEL_PROBE_POLL_S:
                last_reconcile = now
                conn = self._read()
                try:
                    sid = conn.execute(
                        "SELECT session_id FROM attempts WHERE attempt_id"
                        " = ?", (view["active_attempt_id"],)).fetchone()
                finally:
                    conn.close()
                if sid is not None and sid["session_id"]:
                    budget = min(remaining, CONTROL_TIMEOUT_S)
                    try:
                        self._reconcile_bounded(sid["session_id"], budget)
                    except (BackendTimeout, BackendUnavailable):
                        pass  # §4: a slow backend must NOT extend wait
                    if self._clock() >= deadline:
                        break  # budget spent; return wait_expired now
            time.sleep(min(self.config.wait_poll_s, remaining))
        return self._envelope("worker_wait",
                              data={"job": view, "wait_expired": True})

    # ------------------------------------------------------------- tool: result

    def _tool_result(self, a, principal):
        conn = self._read()
        try:
            view = self._job_view_conn(conn, a["job_id"],
                                       principal=principal)
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (a["job_id"],)).fetchone()
            if job["result_revision"] is None:
                raise WorkerError(
                    "RESULT_NOT_READY",
                    "no committed result snapshot yet; check"
                    " worker_status/worker_wait")
            revision = job["result_revision"]
            att = self._latest_attempt(conn, a["job_id"])
            if a.get("attempt_id") and a["attempt_id"] != att["attempt_id"]:
                raise WorkerError("STALE_RESULT",
                                  "attempt_id is not the current attempt")
            cursor_payload = None
            if a.get("cursor"):
                cursor_payload = self._cursor_box.open(
                    a["cursor"], tool="worker_result")
                if cursor_payload.get("principal") != principal \
                        or cursor_payload.get("job") != a["job_id"] \
                        or cursor_payload.get("section") != a["section"] \
                        or cursor_payload.get("artifact") \
                        != a.get("artifact_id"):
                    raise WorkerError("CURSOR_INVALID",
                                      "cursor belongs to a different"
                                      " request context")
                c_rev = cursor_payload.get("result_revision")
                if c_rev is not None and c_rev != revision:
                    raise WorkerError("STALE_RESULT",
                                      "cursor references an older result"
                                      " revision — refetch first page")
            section = a["section"]
            offset = int(cursor_payload.get("next_offset", 0)) \
                if cursor_payload else 0
            items, next_cursor, truncated = [], None, False
            if section == "summary":
                row = self._export_row(conn, a["job_id"], revision,
                                       "summary/summary.txt")
                if row is None:
                    raise WorkerError("EVIDENCE_INCOMPLETE",
                                      "summary evidence is missing")
                text = self._read_export_text(row)
                trunc = len(text) > 16000
                items = [{"kind": "summary", "text": text[:16000],
                          "verification_status": job["verification_status"],
                          "truncated": bool(trunc)}]
                truncated = bool(trunc)
            elif section == "checks":
                rows = conn.execute(
                    "SELECT * FROM checks WHERE job_id = ? ORDER BY"
                    " started_at LIMIT ? OFFSET ?",
                    (a["job_id"], a["limit"], offset)).fetchall()
                for r in rows[offset:offset + a["limit"]]:
                    items.append({
                        "kind": "check",
                        "command": " ".join(json.loads(r["command_json"])),
                        "cwd": r["cwd"], "exit_code": r["exit_code"],
                        "verdict": r["verdict"],
                        "source_snapshot_sha256": r["source_snapshot_sha256"],
                        "evidence_artifact_id": None})
                next_cursor = self._result_cursor(principal, a["job_id"],
                                                  section, None, revision,
                                                  offset + a["limit"]) \
                    if len(rows) > offset + a["limit"] else None
            elif section == "changes":
                row = self._export_row(conn, a["job_id"], revision,
                                       "changes/changes.json")
                if row is None:
                    raise WorkerError("EVIDENCE_INCOMPLETE",
                                      "changes evidence is missing")
                try:
                    payload = json.loads(self._read_export_text(row))
                except (ValueError, OSError):
                    raise WorkerError("EVIDENCE_INCOMPLETE",
                                      "changes evidence unreadable")
                all_changes = payload.get("changes", [])
                page = all_changes[offset:offset + a["limit"]]
                items = [self._contract_change(c) for c in page]
                if len(all_changes) > offset + a["limit"]:
                    next_cursor = self._result_cursor(
                        principal, a["job_id"], section, None, revision,
                        offset + a["limit"])
                truncated = bool(payload.get("truncated", False)) \
                    if isinstance(payload, dict) else False
            elif section == "artifacts":
                rows = conn.execute(
                    "SELECT * FROM artifacts WHERE job_id = ?"
                    " AND result_revision = ? AND visibility = 'export'"
                    " ORDER BY artifact_id LIMIT ? OFFSET ?",
                    (a["job_id"], revision, a["limit"] + 1,
                     offset)).fetchall()
                page = rows[:a["limit"]]
                for r in page:
                    items.append({
                        "kind": "artifact", "artifact_id": r["artifact_id"],
                        "media_type": r["media_type"],
                        "bytes": r["byte_length"], "sha256": r["sha256"],
                        "redacted": bool(r["redacted"]),
                        "complete": bool(r["complete"]),
                        "applyable": bool(r["applyable"])})
                if len(rows) > a["limit"]:
                    next_cursor = self._result_cursor(
                        principal, a["job_id"], section, None, revision,
                        offset + a["limit"])
            else:  # content
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (a["artifact_id"],)).fetchone()
                if row is None or row["job_id"] != a["job_id"] \
                        or row["visibility"] != "export" \
                        or row["result_revision"] != revision:
                    raise WorkerError("NOT_FOUND", "artifact not found")
                blob = self._read_export_bytes(row)
                total = len(blob)
                limit = min(a["byte_limit"], 65536)
                chunk = blob[offset:offset + limit]
                last = offset + len(chunk) >= total
                items = [{
                    "kind": "chunk", "artifact_id": row["artifact_id"],
                    "encoding": "base64",
                    "data_base64": base64.b64encode(chunk).decode(),
                    "offset": offset, "bytes": len(chunk),
                    "total_bytes": total,
                    "sha256_chunk": hashlib.sha256(chunk).hexdigest(),
                    "sha256_artifact": row["sha256"], "last": last}]
                truncated = False
                if not last:
                    next_cursor = self._result_cursor(
                        principal, a["job_id"], section, a["artifact_id"],
                        revision, offset + len(chunk))
            return self._envelope("worker_result", data={
                "job": view, "result_revision": revision,
                "section": section, "items": items,
                "next_cursor": next_cursor, "truncated": bool(truncated)})
        finally:
            conn.close()

    _CHANGE_KINDS = {"added", "modified", "deleted", "renamed",
                     "type_changed"}
    _FILE_TYPES = {"file", "binary", "symlink", "gitlink"}
    _FT_MAP = {"link": "symlink", "deleted": "file", "unknown": "file"}

    @classmethod
    def _contract_change(cls, entry: dict) -> dict:
        """Project P3 change entries onto the contract enums without
        inventing hashes: file_type 'deleted'/'unknown' have no contract
        equivalent and map to 'file' (documented in P4 HANDOFF); kind
        falls back to 'modified' only for display, hashes unchanged."""
        kind = entry.get("change_kind")
        ft = entry.get("file_type")
        if ft in cls._FT_MAP:
            ft = cls._FT_MAP[ft]
        if ft not in cls._FILE_TYPES:
            ft = "file"
        def _sha(v):
            return v if isinstance(v, str) and len(v) == 64 else None
        return {
            "kind": "change",
            "path": entry.get("relative_path") or entry.get("path") or "",
            "old_path": entry.get("old_path"),
            "change_kind": kind if kind in cls._CHANGE_KINDS else "modified",
            "file_type": ft,
            "before_sha256": _sha(entry.get("before_sha256")),
            "after_sha256": _sha(entry.get("after_sha256")),
            "artifact_id": entry.get("artifact_id"),
        }

    def _result_cursor(self, principal, job_id, section, artifact_id,
                       revision, offset) -> str:
        return self._cursor_box.make({
            "principal": principal, "tool": "worker_result",
            "job": job_id, "section": section, "artifact": artifact_id,
            "result_revision": revision, "next_offset": offset,
            "snapshot_id": None, "filters_hash": "",
            "expires_at": self._time() + CURSOR_TTL_S})

    def _export_root(self) -> str:
        return os.path.join(self.config.artifacts_dir, "export")

    def _read_export_bytes(self, row) -> bytes:
        path = self._artifact_export_path(row)
        try:
            return open(path, "rb").read()
        except OSError:
            raise WorkerError("EVIDENCE_INCOMPLETE",
                              "artifact content unavailable")

    def _read_export_text(self, row) -> str:
        return self._read_export_bytes(row).decode("utf-8", "replace")

    def _artifact_export_path(self, row) -> str:
        # P3 ArtifactStore layout: <root>/export/<attempt>/<relative_path>
        return os.path.join(self.config.artifacts_dir, "export",
                            row["attempt_id"], row["relative_path"])

    # ---------------------------------------------------------- tool: followup

    def _tool_followup(self, a, principal):
        conn = self._read()
        try:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (a["job_id"],)).fetchone()
        finally:
            conn.close()
        if job is None:
            raise WorkerError("NOT_FOUND", "job not found")
        if job["principal_id"] != principal:
            raise WorkerError("NOT_FOUND", "job not found")

        # replay first: journal lookup inside followup_intake, but mode
        # and attempt checks below must not block a replayed request.
        existing = self._request_row(principal, a["request_id"])
        if existing is not None:
            if existing["tool"] != "worker_followup" \
                    or existing["payload_sha256"] != payload_sha256(
                        canonical_json(a)):
                raise WorkerError(
                    "IDEMPOTENCY_CONFLICT",
                    "request_id already accepted with a different payload")
            view = self._read_job_view(a["job_id"], principal)
            return self._envelope("worker_followup",
                                  request_id=a["request_id"], replayed=True,
                                  data={"job": view})

        if a["expected_attempt_id"] != job["active_attempt_id"]:
            raise WorkerError("STALE_ATTEMPT",
                              "expected_attempt_id is not the job's"
                              " current attempt")
        self._check_no_ambiguous(a["job_id"])

        if a.get("delivery") is not None:
            return self._followup_busy_delivery(job, a, principal)

        mode = a["mode"]
        state = job["state"]
        if mode == "continue":
            if state != "completed":
                raise WorkerError("JOB_BUSY", "continue requires a completed"
                                              " job (state=" + state + ")")
            session = self._latest_session(a["job_id"])
            if session:
                try:
                    r = self._reconcile_bounded(session, CONTROL_TIMEOUT_S)
                except BackendTimeout:
                    raise WorkerError(
                        "BACKEND_TIMEOUT",
                        "backend session state unclear; continue refused")
                except BackendUnavailable:
                    raise WorkerError(
                        "BACKEND_UNAVAILABLE",
                        "backend unreachable; continue refused")
                if r is None:
                    raise WorkerError(
                        "BACKEND_TIMEOUT",
                        "backend observation exceeded the control"
                        " budget; continue refused (never extended)")
                if r == "pending":
                    raise WorkerError(
                        "APPROVAL_REQUIRED",
                        "backend has a pending permission; resolve the"
                        " exact pending approval before continuing")
                if r != "idle":
                    raise WorkerError(
                        "BACKEND_UNAVAILABLE" if r in ("missing", "unknown")
                        else "JOB_BUSY",
                        f"backend idle not confirmed ({r})")
        elif mode == "retry":
            if state == "waiting_model_selection":
                raise WorkerError(
                    "JOB_BUSY", "use select_model while the job waits for a"
                                " model decision; retry keeps the model")
            if state != "failed":
                raise WorkerError("JOB_BUSY", "retry requires a failed job")
            self._baseline_or_changed(a["job_id"])
        elif mode == "select_model":
            if state != "waiting_model_selection":
                reason = ("waiting_approval is not a model-switch means"
                          if state == "waiting_approval"
                          else f"select_model requires"
                               f" waiting_model_selection (state={state})")
                raise WorkerError("JOB_BUSY", reason)
            self.catalog.check(a["model"])
            if not self.backend.available(a["model"]):
                raise WorkerError("MODEL_UNAVAILABLE",
                                  "selected model is not currently"
                                  " available on the backend")
            self._select_model_decisions[a["job_id"]] = {
                "model": a["model"],
                "user_decision_ref": a["user_decision_ref"],
                "at": self._time()}
        payload = {"user_request_ref": a["user_request_ref"],
                   "mode": mode, "message": a["message"]}
        if mode == "select_model":
            payload["model"] = a["model"]
            payload["user_decision_ref"] = a["user_decision_ref"]
        try:
            if mode == "select_model":
                with self._db():
                    receipt, replayed = self._select_model_intake(
                        principal, a, payload)
                    if not replayed:
                        self.journal.mark_prepared(principal,
                                                   a["request_id"])
            else:
                with self._db():
                    receipt, replayed = self.journal.followup_intake(
                        principal, a["request_id"], a["job_id"], payload,
                        billing_class=self.catalog.billing_class(
                            payload.get("model",
                                        job["requested_model"])),
                        binary_sha256=(self.catalog.engine
                                       .get("binary_sha256") or "0" * 64))
                    self.journal.mark_prepared(principal, a["request_id"])
        except BusyError as e:
            raise WorkerError("JOB_BUSY", _sanitize(str(e)))
        except IdempotencyConflictError as e:
            raise WorkerError("IDEMPOTENCY_CONFLICT", _sanitize(str(e)))
        except NotFoundError:
            raise WorkerError("NOT_FOUND", "job not found")
        except StorageFullError:
            raise WorkerError("STORAGE_FULL", "storage full")
        except DatabaseLockedError:
            raise WorkerError("INTERNAL", "database locked; safe-fail")
        except InvariantFailureError as e:
            raise WorkerError("INTERNAL", _sanitize(str(e)))
        new_attempt = receipt["attempt_id"]
        # followup dispatches a NEW attempt run under this request
        self._start_followup_runner(principal, a["request_id"],
                                    a["job_id"], new_attempt, mode, payload)
        view = self._read_job_view(a["job_id"], principal)
        return self._envelope("worker_followup",
                              request_id=a["request_id"], replayed=replayed,
                              data={"job": view})

    def _followup_busy_delivery(self, job, a, principal) -> dict:
        """Q2 explicit queued followup (coordinator-frozen shape):
        mode=continue + delivery ∈ {steer, queue}, admitted ONLY while
        the job runs with a live session on its active attempt
        (expected_attempt_id was already CAS-checked above, so the
        injection can never target another attempt/job — isolation).
        It is one backend control call: NO new attempt, NO job
        transition, NO runner, and the supervisor NEVER auto-starts or
        adopts the deferred (queue) turn — the backend owns it. Absent
        delivery keeps M1 verbatim: running job -> JOB_BUSY, never an
        internally created queue."""
        if job["state"] != "running":
            raise WorkerError(
                "INVALID_ARGUMENT",
                f"delivery requires a running job (state={job['state']});"
                " use continue/retry/select_model for this state")
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT a.session_id AS sid FROM attempts a"
                " JOIN jobs j ON j.active_attempt_id = a.attempt_id"
                " WHERE j.job_id = ?", (a["job_id"],)).fetchone()
        finally:
            conn.close()
        session = row["sid"] if row is not None else None
        if not session:
            raise WorkerError(
                "BACKEND_UNAVAILABLE",
                "running job has no live session on its active attempt;"
                " delivery refused (never queued internally)")
        # accepted-phase journal row (cancel/approve discipline: the
        # delivery request never enters the dispatch machine). The
        # normalized payload IS the durable acceptance record.
        _, replayed = self._request_intake_simple(
            principal, a["request_id"], "worker_followup", dict(a),
            job_id=a["job_id"], attempt_id=a["expected_attempt_id"])
        if replayed:  # concurrent twin; no second injection
            view = self._read_job_view(a["job_id"], principal)
            return self._envelope("worker_followup",
                                  request_id=a["request_id"],
                                  replayed=True, data={"job": view})
        try:
            resp = self._prompt_bounded(session, a["message"],
                                        a["delivery"], CONTROL_TIMEOUT_S)
        except BackendTimeout:
            # §4: budget spent, never extended, no auto re-send. The
            # request stays 'accepted' (crash-window semantics: the
            # outcome is ambiguous, restart reconciliation surfaces
            # it). Retry with the SAME request_id replays; it never
            # re-injects.
            raise WorkerError(
                "BACKEND_TIMEOUT",
                "prompt delivery exceeded the control budget; never"
                " extended, no auto re-send")
        except BackendUnavailable:
            raise WorkerError("BACKEND_UNAVAILABLE",
                              "backend unreachable for prompt delivery")
        if resp is None or not getattr(resp, "ok", False):
            raise WorkerError(
                "BACKEND_UNAVAILABLE",
                "backend refused the delivery call; request stays"
                " journaled, delivery NOT confirmed")
        self._request_complete(principal, a["request_id"])
        view = self._read_job_view(a["job_id"], principal)
        return self._envelope("worker_followup",
                              request_id=a["request_id"], replayed=False,
                              data={"job": view})

    def _prompt_bounded(self, session_id, text, delivery, budget_s):
        """§4: the delivery is ONE control call on a daemon thread with
        a hard wall bound; a slow backend must NEVER extend the
        response budget (CONTROL_CALLS_MAX=1: no auto re-send). A
        NotImplementedError from the backend is surfaced, never
        swallowed as a silent drop."""
        box = {"value": None, "error": None}

        def run():
            try:
                box["value"] = self.backend.prompt(session_id, text,
                                                   delivery)
            except NotImplementedError:
                box["error"] = BackendUnavailable()
            except (BackendTimeout, BackendUnavailable) as e:
                box["error"] = e
            except Exception:
                box["error"] = BackendUnavailable()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=budget_s)
        if t.is_alive():
            raise BackendTimeout("prompt delivery budget spent")
        if box["error"] is not None:
            raise box["error"]
        return box["value"]

    def _select_model_intake(self, principal, a, payload) -> tuple:
        """select_model intake (T17). P1 followup_intake admits only
        completed/failed baselines; waiting_model_selection→starting is
        legal on the state graph (states.json) and is the ONLY model
        switch means, so this P4 path mirrors the journal discipline:
        attempt row + journaled transition + active pointer + request
        row in ONE transaction, replay/conflict semantics identical."""
        sha = payload_sha256(canonical_json(payload))
        conn = self.store.conn
        existing = self.journal._lookup(conn, principal, a["request_id"])
        if existing is not None:
            self.journal._check_conflict(existing, "worker_followup", sha)
            return self.journal._receipt(existing), True
        job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                           (a["job_id"],)).fetchone()
        if job is None:
            raise NotFoundError("job not found")
        if job["state"] != "waiting_model_selection":
            raise WorkerError(
                "JOB_BUSY", "select_model requires"
                            " waiting_model_selection"
                            f" (state={job['state']})")
        new_attempt_id = new_id("att")
        ordinal = conn.execute(
            "SELECT MAX(ordinal) FROM attempts WHERE job_id = ?",
            (a["job_id"],)).fetchone()[0] + 1
        self.store.insert_attempt(
            conn, attempt_id=new_attempt_id, job_id=a["job_id"],
            ordinal=ordinal, executor_id="local", model=a["model"],
            billing_class=self.catalog.billing_class(a["model"]),
            state="starting", contract_revision=CONTRACT_VERSION,
            binary_sha256=(self.catalog.engine.get("binary_sha256")
                           or "0" * 64))
        self.store._transition_job_conn(
            conn, a["job_id"], job["version"], "starting", "select_model",
            attempt_id=new_attempt_id, now=utc_now())
        self.store.set_active_attempt(conn, a["job_id"], new_attempt_id,
                                      job["version"] + 1, utc_now())
        receipt = {"request_id": a["request_id"], "job_id": a["job_id"],
                   "attempt_id": new_attempt_id, "phase": "accepted",
                   "payload_sha256": sha, "replayed": False}
        self.store.insert_request(
            conn, principal_id=principal, request_id=a["request_id"],
            tool="worker_followup", payload_sha256=sha,
            normalized_json=canonical_json(payload), job_id=a["job_id"],
            attempt_id=new_attempt_id, phase="accepted", receipt=receipt,
            created_at=utc_now(), updated_at=utc_now())
        return receipt, False

    def _check_no_ambiguous(self, job_id) -> None:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT request_id FROM requests WHERE job_id = ?"
                " AND phase = 'ambiguous' LIMIT 1", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            raise WorkerError(
                "DISPATCH_AMBIGUOUS",
                "a dispatch for this job ended in an unproven state"
                " after restart; automatic re-dispatch is forbidden"
                " until reconciled", details={"request_id": row["request_id"]})

    def _baseline_or_changed(self, job_id) -> None:
        att = self._latest_attempt_read(job_id)
        job = self._job_row(job_id)
        try:
            repo = ws.verify_repository(job["repository_path"])
            now_sha = ws.resolve_ref(repo, job["base_revision"] or "HEAD")
        except ws.WorkspaceError:
            return
        intent = self._last_failure_reason(job_id)
        if intent == "WORKSPACE_CHANGED" or (
                job["base_revision"] and now_sha != job["base_revision"]):
            raise WorkerError(
                "WORKSPACE_CHANGED",
                "baseline moved; start a new job against the new"
                " revision instead of retrying")

    def _last_failure_reason(self, job_id) -> str | None:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT reason_code FROM state_events WHERE job_id = ?"
                " AND to_state = 'failed' ORDER BY record_version DESC"
                " LIMIT 1", (job_id,)).fetchone()
            return row["reason_code"] if row else None
        finally:
            conn.close()

    def _latest_session(self, job_id) -> str | None:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT session_id FROM attempts WHERE job_id = ?"
                " ORDER BY ordinal DESC LIMIT 1", (job_id,)).fetchone()
            return row["session_id"] if row and row["session_id"] else None
        finally:
            conn.close()

    def _read_job_view(self, job_id, principal) -> dict:
        conn = self._read()
        try:
            return self._job_view_conn(conn, job_id, principal=principal)
        finally:
            conn.close()

    def _start_followup_runner(self, principal, request_id, job_id,
                               attempt_id, mode, payload) -> None:
        with self._sig:
            if job_id in self._runners:
                return
            t = threading.Thread(target=self._run_followup,
                                 args=(principal, request_id, job_id,
                                       attempt_id, mode, payload),
                                 daemon=True)
            self._runners[job_id] = t
            t.start()

    def _run_followup(self, principal, request_id, job_id, attempt_id, mode,
                      payload) -> None:
        cancel_ev = threading.Event()
        self._cancel_events[job_id] = cancel_ev
        run_obj = None
        try:
            with self._db():
                cur = self.store.get_request(principal, request_id)
                if cur is None or cur["phase"] != "prepared":
                    # Crash re-entry / claimed elsewhere / reconciled:
                    # only a still-'prepared' followup may be dispatched,
                    # and only through the CAS below.
                    return
                claimed = self.journal.claim_dispatch(principal, request_id)
            if not claimed:
                return
            with self._sig:
                self._current_job_id = job_id
            job = self._job_row(job_id)
            model = payload.get("model") or job["requested_model"]
            if not (self.catalog.available(model)
                    and self.backend.available(model)):
                self._enter_model_selection(job_id, job["version"],
                                            attempt_id, model)
                return
            self._update_attempt_state(attempt_id, "starting")
            with self._db_write:
                self.journal.mark_acknowledged(principal, request_id)
            try:
                run_obj = self.backend.start(
                    job_id=job_id, attempt_id=attempt_id, model=model,
                    cwd=self._attempt_cwd(attempt_id) or job["repository_path"],
                    prompt=payload.get("message", ""))
            except ModelUnavailableError:
                self._enter_model_selection(job_id,
                                            self._job_row(job_id)
                                            ["version"], attempt_id, model)
                return
            except (BackendUnavailable, ConnectionError, OSError):
                self._fail_job(job_id, self._job_version(job_id),
                               "BACKEND_UNAVAILABLE", "backend start failed")
                return
            except ContractMismatch as e:
                self._fail_job(job_id, self._job_version(job_id),
                               "CONTRACT_MISMATCH", _sanitize(str(e)))
                return
            with self._db_write:
                self.store.conn.execute(
                    "UPDATE attempts SET session_id = ?, started_at = ?"
                    " WHERE attempt_id = ?",
                    (run_obj.session_id, utc_now(), attempt_id))
            job = self._job_row(job_id)
            self._cas(job_id, job["version"], "running", "followup_started",
                      attempt_id=attempt_id)
            self._update_attempt_state(attempt_id, "running")
            outcome = self._await_run(job_id, attempt_id, run_obj, cancel_ev)
            if outcome is None:
                return
            v = {"goal": job["goal"], "profile": job["profile"],
                 "verification_commands": [], "model": model,
                 "context_files": [], "include_existing_changes": False,
                 "committed_only": False, "workspace": {},
                 "done_when": [], "constraints": [],
                 "user_request_ref": payload.get("user_request_ref", "")}
            if outcome.verdict == VERDICT_COMPLETED:
                self._finish_completed(job_id, attempt_id, run_obj, v,
                                       outcome, None)
            else:
                self._finish_failed(job_id, attempt_id, outcome,
                                    "RUN_FAILED")
            self._request_complete(principal, request_id)
        except Exception:
            self._log.exception("followup runner failure")
            try:
                self._fail_job(job_id, self._job_version(job_id),
                               "INTERNAL", "runner internal failure")
            except Exception:
                pass
        finally:
            try:
                self._request_finish(principal, request_id,
                                     self._read_job_state(job_id))
            except Exception:
                pass
            with self._sig:
                self._runners.pop(job_id, None)
                if self._current_job_id == job_id:
                    self._current_job_id = None
            self._cancel_events.pop(job_id, None)

    def _attempt_cwd(self, attempt_id) -> str | None:
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT workspace_path FROM attempts WHERE attempt_id = ?",
                (attempt_id,)).fetchone()
        finally:
            conn.close()
        return row["workspace_path"] if row else None

    # ------------------------------------------------------------- tool: cancel

    def _tool_cancel(self, a, principal):
        # Replay-first intake: the stale/busy/terminal evaluations run
        # ONLY for a new request and inside the intake tx (before the
        # journal row lands), so an accepted cancel replays with its
        # original receipt even after the attempt has moved on.
        def pre_check(conn):
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (a["job_id"],)).fetchone()
            if job is None or job["principal_id"] != principal:
                raise WorkerError("NOT_FOUND", "job not found")
            if a["expected_attempt_id"] != job["active_attempt_id"]:
                raise WorkerError(
                    "STALE_ATTEMPT",
                    "expected_attempt_id is not the job's current attempt")
            if job["state"] == "cancel_requested":
                raise WorkerError(
                    "CANCEL_UNCONFIRMED",
                    "a cancel is already in flight and the backend stop"
                    " is not yet confirmed", details={"job_id": a["job_id"]})
            if job["state"] == "needs_attention" and (
                    job["attention_reason"] or "").startswith(
                        "CANCEL_UNCONFIRMED"):
                raise WorkerError(
                    "CANCEL_UNCONFIRMED",
                    "backend stop still unconfirmed; the workspace lease"
                    " is retained (needs_attention)", details={"job_id": a["job_id"]})

        row, replayed = self._request_intake_simple(
            principal, a["request_id"], "worker_cancel", a,
            job_id=a["job_id"], attempt_id=a["expected_attempt_id"],
            pre_accept_check=pre_check)
        view = self._read_job_view(a["job_id"], principal)
        if replayed:
            return self._envelope("worker_cancel",
                                  request_id=a["request_id"], replayed=True,
                                  data={"job": view,
                                        "cancel_requested": True})
        job = self._job_row(a["job_id"])
        if job["state"] in TERMINAL_JOB_STATES:
            self._request_complete(principal, a["request_id"])
            return self._envelope("worker_cancel",
                                  request_id=a["request_id"],
                                  data={"job": view,
                                        "cancel_requested": False})
        if job["state"] == "needs_attention":
            # No runner owns this job; the cancel itself must observe and
            # stop the backend (§8 exact-attempt), with a reconcile proof
            # (state.py: needs_attention cannot auto-dispatch).
            self._cancel_needs_attention(job, a, principal)
            return self._envelope("worker_cancel",
                                  request_id=a["request_id"],
                                  data={"job": self._read_job_view(
                                      a["job_id"], principal),
                                      "cancel_requested": True})
        try:
            self._cas(a["job_id"], job["version"], "cancel_requested",
                      "cancel_requested", attempt_id=job["active_attempt_id"])
        except CasConflictError:
            raise WorkerError("CANCEL_UNCONFIRMED",
                              "cancel could not be recorded atomically;"
                              " re-check status")
        except InvariantFailureError:
            raise WorkerError("CANCEL_UNCONFIRMED",
                              "cancel could not be recorded; job moved"
                              " underneath us")
        ev = self._cancel_events.get(a["job_id"])
        if ev is not None:
            ev.set()  # runner confirms the exact session stop
        else:
            # runner died without terminal state — stop it ourselves
            self._cancel_without_runner(job, principal)
        view = self._read_job_view(a["job_id"], principal)
        self._request_complete(principal, a["request_id"])
        return self._envelope("worker_cancel", request_id=a["request_id"],
                              data={"job": view, "cancel_requested": True})

    def _backend_for_attempt(self, attempt_id):
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT session_id FROM attempts WHERE attempt_id = ?",
                (attempt_id,)).fetchone()
        finally:
            conn.close()
        if row is None or not row["session_id"]:
            return None
        return BackendRun(row["session_id"])

    def _reconcile_bounded(self, session_id, timeout_s):
        """MCP.md §4: a slow backend must NEVER extend a call-response
        budget. reconcile runs on a daemon thread and the caller walks
        away at the wall bound (the stray observation keeps no lock and
        touches no state — it is an observation only)."""
        box = {"value": None, "error": None}

        def run():
            try:
                box["value"] = self.backend.reconcile(session_id,
                                                      timeout_s=timeout_s)
            except (BackendTimeout, BackendUnavailable) as e:
                box["error"] = e
            except Exception:
                box["error"] = BackendUnavailable()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=timeout_s)
        if t.is_alive():
            return None
        if box["error"] is not None:
            raise box["error"]
        return box["value"]

    def _reconcile_proof(self, session_id) -> str:
        try:
            r = self._reconcile_bounded(session_id, CONTROL_TIMEOUT_S)
        except BackendTimeout:
            raise WorkerError("BACKEND_TIMEOUT",
                              "backend state observation timed out")
        except BackendUnavailable:
            raise WorkerError("BACKEND_UNAVAILABLE",
                              "backend unreachable")
        if r is None:
            raise WorkerError("BACKEND_TIMEOUT",
                              "backend observation exceeded the"
                              " control budget; never extended")
        return f"reconcile={r}"

    def _cancel_needs_attention(self, job, a, principal) -> None:
        run = self._backend_for_attempt(job["active_attempt_id"])
        proof = self._reconcile_proof(
            run.session_id) if run else "reconcile=no-backend-session"
        self._cas(job["job_id"], job["version"], "cancel_requested",
                  "cancel_requested", reconcile_proof=proof,
                  attempt_id=job["active_attempt_id"])
        if run is None:
            self._cas(job["job_id"], self._job_row(job["job_id"])
                      ["version"], "cancelled", "cancel_confirmed_no_backend",
                      attempt_id=job["active_attempt_id"])
            self._attempt_close(job["active_attempt_id"], "cancelled")
            return
        res = self.backend.cancel(run)
        job2 = self._job_row(job["job_id"])
        if res.confirmed:
            self._cas(job["job_id"], job2["version"], "cancelled",
                      "cancel_confirmed", attempt_id=job["active_attempt_id"])
            self._attempt_close(job["active_attempt_id"], "cancelled")
            self._release_lease_for(job["job_id"])
        else:
            self._cas(job["job_id"], job2["version"], "needs_attention",
                      "attention", attempt_id=job["active_attempt_id"],
                      attention_reason="CANCEL_UNCONFIRMED:"
                                       f" {_sanitize(res.reason)}"
                                       " (workspace lease retained)")

    def _cancel_without_runner(self, job, principal) -> None:
        run = self._backend_for_attempt(job["active_attempt_id"])
        if run is None:
            self._attempt_close(job["active_attempt_id"], "cancelled")
            self._cas(job["job_id"],
                      self._job_version(job["job_id"]),
                      "cancelled", "cancel_confirmed_no_backend",
                      attempt_id=job["active_attempt_id"])
            self._release_lease_for(job["job_id"])
            return
        res = self.backend.cancel(run)
        job2 = self._job_row(job["job_id"])
        if res.confirmed:
            self._attempt_close(job["active_attempt_id"], "cancelled")
            self._cas(job["job_id"], job2["version"], "cancelled",
                      "cancel_confirmed", attempt_id=job["active_attempt_id"])
            self._release_lease_for(job["job_id"])
        else:
            self._needs_attention(
                job["job_id"], job2["version"],
                f"CANCEL_UNCONFIRMED: {_sanitize(res.reason)}"
                " (workspace lease retained)", job["active_attempt_id"])

    # ------------------------------------------------------------- tool: approve

    def _tool_approve(self, a, principal):
        conn = self._read()
        try:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?",
                               (a["job_id"],)).fetchone()
            row = conn.execute("SELECT * FROM approvals WHERE approval_id"
                               " = ?", (a["approval_id"],)).fetchone()
            if (job is None or row is None or row["job_id"] != a["job_id"]
                    or job["principal_id"] != principal):
                raise WorkerError("NOT_FOUND", "approval not found")
        finally:
            conn.close()
        backend_request_id = row["backend_request_id"]
        grant = ApprovalGrant(
            approval_id=a["approval_id"], attempt_id=a["attempt_id"],
            backend_request_id=backend_request_id, principal=principal,
            scope_sha256=row["scope_sha256"], generation=row["generation"],
            expires_at=datetime.strptime(
                row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp(),
            channel_verified=self.config.approval_channel_verified,
            decision="once" if a["decision"] == "approve" else "reject",
            challenge=a["challenge"])
        with self._db_write:
            req_row, replayed = self._request_intake_simple(
                principal, a["request_id"], "worker_approve", a,
                job_id=a["job_id"], attempt_id=a["attempt_id"])
        if replayed:
            view = self._read_job_view(a["job_id"], principal)
            return self._envelope("worker_approve",
                                  request_id=a["request_id"], replayed=True,
                                  data={"job": view,
                                        "approval_id": a["approval_id"],
                                        "decision_recorded": True})
        try:
            validate_and_reply(
                self._approval_adapter(), row["session_id"], grant,
                key=self._approval_key,
                current_attempt_id=job["active_attempt_id"],
                journal=self._approval_journal,
                now=self._time())
        except ApprovalError as e:
            raise WorkerError(e.code if e.code in ERROR_CODES
                              else "APPROVAL_INVALID",
                              str(e), details=e.detail)
        with self._db_write:
            self.store.conn.execute(
                "UPDATE approvals SET status = ?, user_decision_ref = ?,"
                " decision_origin = 'mcp', decided_at = ? WHERE approval_id"
                " = ?",
                ("approved" if a["decision"] == "approve" else "denied",
                 a["user_decision_ref"], utc_now(), a["approval_id"]))
        if job["state"] == "waiting_approval":
            try:
                self._cas(a["job_id"], job["version"], "running",
                          "approval_decided", attempt_id=a["attempt_id"])
            except (CasConflictError, InvariantFailureError):
                pass
        self._request_complete(principal, a["request_id"])
        view = self._read_job_view(a["job_id"], principal)
        return self._envelope("worker_approve", request_id=a["request_id"],
                              data={"job": view,
                                    "approval_id": a["approval_id"],
                                    "decision_recorded": True})

    def _approval_adapter(self):
        """Adapts SupervisorBackend to the P2 validate_and_reply surface."""
        backend = self.backend

        class _Adapter:
            def pending_permissions(self, session_id):
                return backend.pending_permissions(session_id)

            def reply_permission(self, session_id, request_id, decision):
                ok = backend.reply_permission(session_id, request_id,
                                              decision)
                return ApiResponse(
                    "POST", f"/session/{session_id}/permission/"
                    f"{request_id}/{decision}", 0 if ok else 1,
                    {"ok": ok}, "")
        return _Adapter()

    # ------------------------------------------------------------ operate (Q3)

    def _operate_attempt_is_live(self, attempt_id: str) -> bool:
        """An operate approval may bind ONLY to the currently active,
        non-terminal attempt of some job."""
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT 1 FROM jobs j JOIN attempts a"
                " ON a.attempt_id = j.active_attempt_id"
                " WHERE j.active_attempt_id = ? AND a.state NOT IN"
                " ('completed','failed','cancelled')",
                (attempt_id,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def _handle_operate(self, method, args, principal) -> dict:
        if not principal.startswith("local:") or not \
                principal.split(":")[-1].isdigit() or \
                int(principal.split(":")[-1]) != self._uid:
            return self._error_envelope(method, "UNAUTHENTICATED",
                                        "principal is not this host's"
                                        " local principal")
        try:
            if method == "operate.status":
                opmod.validate_status_args(args)
                data = self._operate_gate.status()
            elif method == "operate.revalidate":
                a = opmod.validate_revalidate_args(args)
                data = self._operate_gate.revalidate(
                    a["record_id"], a["attempt_id"])
            else:
                a = opmod.validate_plan_args(args)
                data = self._operate_gate.plan(
                    a["operation"], attempt_id=a["attempt_id"])
        except opmod.OperateDenied as e:
            return self._error_envelope(method, e.code,
                                        _sanitize(e.message),
                                        details=e.details)
        return {"contract_version": CONTRACT_VERSION, "method": method,
                "observed_at": utc_now(), "data": data}

    # ---------------------------------------------------------------- viewer

    @staticmethod
    def _strip_challenges(jobs: list[dict]) -> list[dict]:
        """§7: challenges never leave through logs/terminals."""
        for j in jobs:
            for pa in j.get("pending_approvals", ()):
                pa["challenge"] = None
        return jobs

    def _viewer_snapshot(self) -> dict:
        conn = self._read()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM state_events"
            ).fetchone()[0]
            return {"jobs": self._strip_challenges(
                        [self._job_row_view(conn, r) for r in rows]),
                    "next_seq": max_seq,
                    "current_job": self._current_job_id}
        finally:
            conn.close()

    def _viewer_tail(self, after_seq: int) -> dict:
        conn = self._read()
        try:
            rows = conn.execute(
                "SELECT seq, job_id, attempt_id, from_state, to_state,"
                " reason_code, observed_at FROM state_events WHERE seq > ?"
                " ORDER BY seq LIMIT 200", (after_seq,)).fetchall()
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM state_events"
            ).fetchone()[0]
            return {"events": [dict(r) for r in rows],
                    "next_seq": max_seq,
                    "jobs": self._strip_challenges(
                        [self._job_row_view(conn, r) for r in conn.execute(
                            "SELECT * FROM jobs ORDER BY created_at DESC"
                            " LIMIT 50").fetchall()]),
                    "current_job": self._current_job_id}
        finally:
            conn.close()

    def _viewer_requests(self, principal: str) -> dict:
        """W6b read-only pending-request API: journal rows still in a
        non-terminal phase (accepted/prepared/sending/acknowledged/
        ambiguous) — the accepted-phase window a BACKEND_TIMEOUT or
        crash can leave open (delivery injections included). Metadata
        only: never normalized_json (carries user message bodies),
        never receipt internals. The viewer can only observe these
        rows; resolving them is supervisor-side reconcile, never a
        viewer mutation."""
        conn = self._read()
        try:
            rows = conn.execute(
                "SELECT request_id, tool, phase, payload_sha256,"
                " created_at, updated_at FROM requests"
                " WHERE principal_id = ?"
                " AND phase NOT IN ('completed','failed')"
                " ORDER BY updated_at LIMIT 200", (principal,)).fetchall()
            return {"requests": [dict(r) for r in rows]}
        finally:
            conn.close()


# bind the instance attributes that must exist before runners use them
def _bootstrap_attrs(cls: type) -> None:
    original_init = cls.__init__

    def __init__(self, *args, **kw):
        self._cancel_events: dict[str, threading.Event] = {}
        self._select_model_decisions: dict[str, dict] = {}
        original_init(self, *args, **kw)
    cls.__init__ = __init__


_bootstrap_attrs(Supervisor)


class _NullLogger:
    def note(self, msg):
        pass

    def exception(self, msg):
        import traceback
        traceback.print_exc(file=sys.stderr)


def build_prompt(v: dict) -> str:
    parts = [v["goal"]]
    if v.get("constraints"):
        parts.append("Constraints:\n- " + "\n- ".join(v["constraints"]))
    if v.get("done_when"):
        parts.append("Done when:\n- " + "\n- ".join(v["done_when"]))
    if v.get("context"):
        parts.append(v["context"])
    return "\n\n".join(parts)


# socketserver without unix sockets raises at import on non-POSIX — Mac
# mini is the target (AGENTS.md); guard the attribute for portability.
if not hasattr(socketserver, "ThreadingUnixStreamServer"):  # pragma: no cover
    raise RuntimeError("POSIX Unix domain sockets are required")
