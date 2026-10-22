"""Normalized event model and OBSERVED envelope mapping (P2).

Grounding (OPENCODE_V2.md §2 mapping rows, M0 evidence chain at commit
e634d6c; public redacted fixtures under tests/golden/raw/ with checksums
in tests/golden/golden_manifest.json):

- EVT-ENVELOPE (OBSERVED_PARTIAL): top-level JSONL envelope keys
  ``type``/``timestamp``/``sessionID``/``part``; observed top-level types
  are ``step_start``, ``text``, ``tool_use``, ``step_finish`` (runs
  20261018T093748Z / 093841Z / 094523Z). Unknown types are preserved as
  kind ``unknown`` — the real unknown-type stream was never observed
  (EVT-ENVELOPE residual), so no handling beyond preserve-and-mark is
  claimed.
- SESSION-ID (OBSERVED): ``sessionID`` (``ses_`` prefix) binds one run to
  one session; ``session-map.json`` rows in the evidence runs.
- TURN-ID (OBSERVED, adopted): turn identity is ``part.messageID``
  (``msg_`` prefix). Busy follow-up is never refused (no reject branch);
  ``delivery`` enum ``[steer, queue]`` — steer injects into the running
  turn (answer contamination observed), queue starts an independent new
  turn after idle.
- TOOL-RESULT (OBSERVED): tool parts carry call ID ``part.id`` in two
  real variants (UUID and ``chatcmpl-tool-…``) plus part ID
  ``part.partID``; status/input/output observed; shell exit observed only
  at ``part.state.metadata.metadata.exit`` — absence is normal, never a
  fabricated 0.
- NORMAL-END (OBSERVED): normal end is stdout EOF + exit 0. ``step_finish``
  with reason ``tool-calls`` is an inter-step boundary, NOT terminal: in
  every real run the final event is a ``text`` part and no ``step_finish``
  follows it (093748Z has no step_finish at all).
- USAGE (OBSERVED): provider-reported ``tokens``/``cost`` ride on
  ``step_finish`` parts; missing step_finish means usage unknown (093748Z).

Synthetic shapes (fake bands, marked SYNTHETIC in the manifest) add the
``error`` envelope with ``error.name``/``error.data``; they are fake-CLI
output, never claimed as real OpenCode output.

This module is pure normalization/judgement: it never launches processes,
never decides product state-machine transitions, and never infers models.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Normalized kinds — the design contract in OPENCODE_V2.md §3.
KIND_SESSION_BOUND = "session_bound"
KIND_TOOL_STARTED = "tool_started"
KIND_TOOL_FINISHED = "tool_finished"
KIND_MESSAGE = "message"
KIND_PERMISSION_REQUESTED = "permission_requested"
KIND_PROVIDER_ERROR = "provider_error"
KIND_EXECUTION_IDLE = "execution_idle"
KIND_USAGE = "usage"
KIND_TRANSPORT_ERROR = "transport_error"
KIND_UNKNOWN = "unknown"

ALL_KINDS = (
    KIND_SESSION_BOUND, KIND_TOOL_STARTED, KIND_TOOL_FINISHED,
    KIND_MESSAGE, KIND_PERMISSION_REQUESTED, KIND_PROVIDER_ERROR,
    KIND_EXECUTION_IDLE, KIND_USAGE, KIND_TRANSPORT_ERROR, KIND_UNKNOWN,
)

# OBSERVED top-level envelope type -> normalized kind. Only the four real
# types plus the synthetic ``error`` shape appear here; anything else is
# kind ``unknown``.
OBSERVED_TYPE_TO_KIND = {
    "step_start": KIND_SESSION_BOUND,
    "text": KIND_MESSAGE,
    "tool_use": KIND_TOOL_FINISHED,
    "step_finish": KIND_USAGE,
    "error": KIND_PROVIDER_ERROR,  # SYNTHETIC shape (fake band "error")
}


def canonical_json(obj: Any) -> str:
    """Stable serialization used for payload-equality dedupe and scope
    hashing. Key order and separators are pinned; non-JSON types raise."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def source_event_id(envelope: dict) -> str | None:
    """Part identity for dedupe.

    Tool parts carry the part ID under ``part.partID`` while ``part.id``
    is the *call* ID (two real variants observed); non-tool parts carry
    the part ID under ``part.id``. Using partID first for tools is
    grounded in the 094523Z/093841Z fixtures.
    """
    part = envelope.get("part")
    if isinstance(part, dict):
        return part.get("partID") or part.get("id")
    return envelope.get("id")


def call_id(envelope: dict) -> str | None:
    part = envelope.get("part")
    if isinstance(part, dict) and part.get("type") == "tool":
        return part.get("id")
    return None


def tool_exit(envelope: dict) -> int | None:
    """Observed shell exit lives at part.state.metadata.metadata.exit.

    Absent for read/edit/etc. — absence is NOT exit 0 (RUNBOOK ch.6:
    never fill missing exit codes from model self-report).
    """
    part = envelope.get("part")
    if not isinstance(part, dict):
        return None
    state = part.get("state")
    if not isinstance(state, dict):
        return None
    meta = state.get("metadata")
    if isinstance(meta, dict):
        inner = meta.get("metadata")
        if isinstance(inner, dict) and isinstance(inner.get("exit"), int):
            return inner["exit"]
    return None


@dataclass(frozen=True)
class NormalizedEvent:
    """Contract §3 fields, one per observed envelope."""
    attempt_id: str
    seq: int
    observed_at: str
    kind: str
    source_kind: str | None
    source_event_id: str | None
    session_id: str | None
    turn_id: str | None
    raw_artifact_id: str | None
    payload: dict


@dataclass(frozen=True)
class DupInfo:
    """Result of a duplicate source ID. ``conflict`` means same ID with a
    DIFFERENT payload — the contract forbids merging those; both are kept
    and the later one is marked in its payload."""
    source_event_id: str
    duplicate: bool
    conflict: bool


class EventNormalizer:
    """Stateful normalizer for one attempt's event stream.

    - dedupe: same source ID + byte-identical canonical payload -> drop
      (counted); same source ID + different payload -> keep both, second
      payload gains ``dup_conflict: true`` (never merged).
    - no source ID: events are never merged by content hash (contract
      §3: content hash alone must not fuse distinct real calls).
    - session/turn identity: first observed ``sessionID`` binds the
      attempt; ``part.messageID`` is the turn ID (TURN-ID adopted row).
    """

    def __init__(self, attempt_id: str, raw_artifact_id: str | None = None):
        self.attempt_id = attempt_id
        self.raw_artifact_id = raw_artifact_id
        self.session_id: str | None = None
        self.turn_ids: list[str] = []
        self.duplicates = 0
        self.dup_conflicts = 0
        self._seq = 0
        self._seen: dict[str, str] = {}

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def note_duplicate(self, info: DupInfo) -> None:
        if info.conflict:
            self.dup_conflicts += 1
        elif info.duplicate:
            self.duplicates += 1

    def check_dup(self, envelope: dict) -> DupInfo | None:
        sid = source_event_id(envelope)
        if sid is None:
            return None
        canon = canonical_json(envelope)
        prev = self._seen.get(sid)
        if prev is None:
            self._seen[sid] = canon
            return None
        if prev == canon:
            return DupInfo(sid, duplicate=True, conflict=False)
        return DupInfo(sid, duplicate=False, conflict=True)

    def normalize(self, envelope: Any, observed_at: str) -> NormalizedEvent:
        """Map one parsed JSON envelope. Non-dict JSON and parse failures
        arrive as ``{"_raw_line": ...}`` sentinels from the capture layer
        and are preserved as kind ``unknown`` (never dropped)."""
        seq = self._next_seq()
        if (isinstance(envelope, dict) and envelope.get("parse_error")
                and "_raw_line" in envelope):
            # Malformed-line sentinel from the capture layer
            # (parse_jsonl_line): preserved and marked, never merged,
            # never a completion.
            return NormalizedEvent(
                attempt_id=self.attempt_id, seq=seq, observed_at=observed_at,
                kind=KIND_UNKNOWN, source_kind=None, source_event_id=None,
                session_id=self.session_id, turn_id=None,
                raw_artifact_id=self.raw_artifact_id,
                payload={"_raw_line": envelope["_raw_line"],
                         "parse_error": True})
        if not isinstance(envelope, dict):
            return NormalizedEvent(
                attempt_id=self.attempt_id, seq=seq, observed_at=observed_at,
                kind=KIND_UNKNOWN, source_kind=None, source_event_id=None,
                session_id=self.session_id, turn_id=None,
                raw_artifact_id=self.raw_artifact_id,
                payload={"_raw_envelope": envelope, "parse_error": True})

        top = envelope.get("type")
        kind = OBSERVED_TYPE_TO_KIND.get(top, KIND_UNKNOWN)
        part = envelope.get("part")
        session_id = envelope.get("sessionID")
        turn_id = None
        if isinstance(part, dict):
            mid = part.get("messageID")
            if isinstance(mid, str):
                turn_id = mid
            if session_id is None:
                session_id = part.get("sessionID")

        if isinstance(session_id, str):
            if self.session_id is None:
                self.session_id = session_id
            if turn_id is not None and turn_id not in self.turn_ids:
                self.turn_ids.append(turn_id)

        payload: dict[str, Any] = {"envelope": envelope}
        if kind == KIND_TOOL_FINISHED and isinstance(part, dict):
            state = part.get("state")
            payload.update({
                "tool": part.get("tool"),
                "call_id": part.get("id"),
                "status": state.get("status") if isinstance(state, dict) else None,
                "exit": tool_exit(envelope),
            })
        elif kind == KIND_USAGE and isinstance(part, dict):
            payload.update({
                "reason": part.get("reason"),
                "cost": part.get("cost"),
                "tokens": part.get("tokens"),
            })
        elif kind == KIND_MESSAGE and isinstance(part, dict):
            payload["text"] = part.get("text")
        elif kind == KIND_PROVIDER_ERROR:
            err = envelope.get("error")
            payload["error_name"] = err.get("name") if isinstance(err, dict) else None
            payload["synthetic_shape"] = True  # fake-band shape only
        elif kind == KIND_UNKNOWN:
            payload["unmapped_type"] = top

        return NormalizedEvent(
            attempt_id=self.attempt_id, seq=seq, observed_at=observed_at,
            kind=kind, source_kind=top if isinstance(top, str) else None,
            source_event_id=source_event_id(envelope),
            session_id=session_id if isinstance(session_id, str) else None,
            turn_id=turn_id, raw_artifact_id=self.raw_artifact_id,
            payload=payload)

    def permission_requested(self, request: dict, observed_at: str) -> NormalizedEvent:
        """Normalize a pending-permission request object from the session
        API (OBSERVED shape, run 20261018T103444Z: id/sessionID/action/
        resources/save/source)."""
        seq = self._next_seq()
        sid = request.get("sessionID")
        if isinstance(sid, str) and self.session_id is None:
            self.session_id = sid
        return NormalizedEvent(
            attempt_id=self.attempt_id, seq=seq, observed_at=observed_at,
            kind=KIND_PERMISSION_REQUESTED, source_kind="permission",
            source_event_id=request.get("id"),
            session_id=sid if isinstance(sid, str) else None,
            turn_id=(request.get("source") or {}).get("messageID")
            if isinstance(request.get("source"), dict) else None,
            raw_artifact_id=self.raw_artifact_id,
            payload={"request": request,
                     "scope_sha256": scope_sha256(request)})


def scope_sha256(request: dict) -> str:
    """Internal binding hash for an approval scope (MCP.md §7
    scope_sha256). Canonical form pins action + sorted resources + sorted
    save; it is our hash, not an OpenCode field."""
    canon = canonical_json({
        "action": request.get("action"),
        "resources": sorted(request.get("resources") or []),
        "save": sorted(request.get("save") or []),
    })
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Completion judgement — OPENCODE_V2.md §4 rules, grounded in NORMAL-END /
# M0-04 bands. This is an execution-end verdict, NOT verification success:
# a caller's own checks (tests, review) are separate (M0-04 checkfail band:
# CLI exit 0 + well-formed stream can still fail the harness check).
# ---------------------------------------------------------------------------

VERDICT_COMPLETED = "COMPLETED"
VERDICT_CLI_FAILED = "CLI_FAILED"
VERDICT_CLI_OK_TOOL_FAILED = "CLI_OK_TOOL_FAILED"
VERDICT_NO_FINAL_ANSWER = "NO_FINAL_ANSWER"
VERDICT_EMPTY_OUTPUT = "EMPTY_OUTPUT"
VERDICT_CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
VERDICT_UNKNOWN = "UNKNOWN"


@dataclass
class CompletionVerdict:
    verdict: str
    reason: str
    usage: Any = "unknown"          # provider-reported dict, or "unknown"
    turn_ids: list[str] = field(default_factory=list)
    failed_tools: list[dict] = field(default_factory=list)
    prev_turn_events: int = 0       # excluded as belonging to earlier turns


def judge_completion(
    events: list[NormalizedEvent],
    *,
    exit_code: int | None,
    eof: bool,
    contract_mismatch: bool = False,
    killed_by_us: bool = False,
    current_turn_ids: set[str] | None = None,
) -> CompletionVerdict:
    """Apply the §4 completion rules to one attempt's normalized events.

    - ``step_finish`` (kind usage) is never a terminal signal; completion
      requires EOF + exit 0 + the last current-turn event being a final
      ``message`` (observed NORMAL-END shape) + no failed tool.
    - ``current_turn_ids`` filters previous-turn events: a prior turn's
      final text or idle must never satisfy the current turn (M0-08
      confusion guard).
    - exit code missing -> UNKNOWN (no fabricated 0); our own kill
      (host timeout) -> UNKNOWN, never ``cancelled`` (that needs exact
      session stop confirmation — opencode_v2.request_cancel).
    """
    if contract_mismatch:
        return CompletionVerdict(VERDICT_CONTRACT_MISMATCH,
                                 "framing over 1MiB or malformed UTF-8; never success")
    if killed_by_us:
        return CompletionVerdict(VERDICT_UNKNOWN,
                                 "host timeout killed our child; stop not confirmed on backend")
    if exit_code is None:
        return CompletionVerdict(VERDICT_UNKNOWN, "exit code missing; no fabricated 0")
    if exit_code != 0:
        return CompletionVerdict(VERDICT_CLI_FAILED, f"CLI exit {exit_code}")

    prev_count = 0
    current: list[NormalizedEvent] = []
    for ev in events:
        if current_turn_ids is not None and ev.turn_id is not None \
                and ev.turn_id not in current_turn_ids:
            prev_count += 1
            continue
        current.append(ev)

    if not current:
        return CompletionVerdict(VERDICT_EMPTY_OUTPUT,
                                 "no events for current turn; never treated as success",
                                 prev_turn_events=prev_count)

    failed = []
    for ev in current:
        if ev.kind == KIND_TOOL_FINISHED:
            status = ev.payload.get("status")
            exit_ = ev.payload.get("exit")
            if (status is not None and status != "completed") or \
               (exit_ is not None and exit_ != 0):
                failed.append({"call_id": ev.payload.get("call_id"),
                               "tool": ev.payload.get("tool"),
                               "status": status, "exit": exit_})
    if failed:
        return CompletionVerdict(VERDICT_CLI_OK_TOOL_FAILED,
                                 "CLI exit 0 but a tool failed; exit 0 is not success",
                                 failed_tools=failed, prev_turn_events=prev_count)

    last = current[-1]
    if last.kind == KIND_MESSAGE:
        usage: Any = "unknown"
        for ev in reversed(current):
            if ev.kind == KIND_USAGE:
                usage = {"reason": ev.payload.get("reason"),
                         "cost": ev.payload.get("cost"),
                         "tokens": ev.payload.get("tokens")}
                break
        turns = sorted({ev.turn_id for ev in current if ev.turn_id})
        if not eof:
            return CompletionVerdict(VERDICT_UNKNOWN,
                                     "stream not at EOF; trailing text alone is not completion",
                                     usage=usage, turn_ids=turns,
                                     prev_turn_events=prev_count)
        return CompletionVerdict(VERDICT_COMPLETED,
                                 "EOF + exit 0 + trailing final message, no failed tool",
                                 usage=usage, turn_ids=turns,
                                 prev_turn_events=prev_count)

    if last.kind == KIND_USAGE:
        return CompletionVerdict(VERDICT_NO_FINAL_ANSWER,
                                 "stream ended on step_finish (reason "
                                 f"{last.payload.get('reason')!r}): step_finish is "
                                 "an inter-step boundary, not terminal",
                                 prev_turn_events=prev_count)
    if last.kind == KIND_TOOL_FINISHED:
        return CompletionVerdict(VERDICT_NO_FINAL_ANSWER,
                                 "stream ended on a tool result; no final message",
                                 prev_turn_events=prev_count)
    if last.kind == KIND_PROVIDER_ERROR:
        return CompletionVerdict(VERDICT_UNKNOWN,
                                 "stream ended on a provider error event; "
                                 "classification is the caller's (PROVIDER-ERROR row)",
                                 prev_turn_events=prev_count)
    return CompletionVerdict(VERDICT_UNKNOWN,
                             f"stream ended on kind {last.kind}; not a completed shape",
                             prev_turn_events=prev_count)
