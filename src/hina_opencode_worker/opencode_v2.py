"""OpenCode V2 adapter — capture, session identity, approval, cancel (P2).

Everything OBSERVED here comes from the M0 evidence chain (commit e634d6c):
the ch.2 capture algorithm (experiments/m0/probe/capture.py), the session
API argv shapes measured on the owned temp serve (run_m005/006/008), and
the JSONL envelope fixtures under tests/golden/raw/. Synthetic parts are
labelled in tests/golden/golden_manifest.json. Nothing here invents an
OpenCode contract:

- CLI-ARGV (OBSERVED_HELP): ``run --model M --format json --title T msg``;
  ``--session`` continue/create semantics stay OPEN_LIVE, so this adapter
  never emits ``run --session`` — follow-ups go through the OBSERVED
  session-API prompt path instead.
- Version pin (§5): binary sha256 + ``--version`` string are checked
  before any launch; a mismatch raises ContractMismatch and blocks
  execution. Models are never inferred or defaulted.
- PERMISSION (OBSERVED, run 20261018T103444Z): pending requests via
  ``GET /api/session/{sid}/permission``; reply decisions are only
  ``once``/``reject`` — ``always`` is never sent.
- INTERRUPT (OBSERVED, run 20261018T104948Z): ``POST .../interrupt`` with
  no body returns ``{"interrupted": true}`` — an ack, not a confirmed
  stop. Cancellation is confirmed only by exact-session activity
  observation (GET /api/session/active); other sessions must survive.
- TURN-ID (OBSERVED, adopted): busy follow-up is never refused; the
  prompt body may carry ``delivery`` from the enum ``[steer, queue]``.
- Approval contract (docs/contracts/MCP.md §7): challenge =
  base64url(HMAC-SHA256(key, canonical payload)), 15-minute TTL, replay
  journal consulted BEFORE TTL/state rechecks, unverified channel blocks
  execution. Error codes: APPROVAL_CHANNEL_UNAVAILABLE, APPROVAL_EXPIRED,
  STALE_ATTEMPT, APPROVAL_INVALID.

Stdlib only (bootstrap: Python 3.12, zero third-party deps).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import selectors
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Callable

from .events import EventNormalizer, NormalizedEvent, canonical_json, scope_sha256

# --- version pin (signed certs: gate d140f23 tested_source_sha b99af5c;
# re-signed v2 7dfedb0 / mirror 875ef1c tested_source_sha 7a2a235 — the
# binary fingerprint and CLI version are identical across both) ---
PINNED_CLI_VERSION = "opencode v2.0.30"
PINNED_BINARY_SHA256 = (
    "ddb556472e044be6d757fbc5fefc25b2edd2896ddbc0d388d6178b7b0a639b96"
)
# The certified mapping is the OPENCODE_V2.md blob recorded in the signed
# gate (v1 recorded 13b9195; v2 transcribed the TURN-ID muse addendum).
MAPPING_REVISION = (
    "0709ae99c88aa9f33e0056657dd6532d17b4685f "
    "(adopted rows per 13b9195; TURN-ID muse addendum 7a2a235)"
)

# ch.1/ch.2 framing limit: a stdout line over 1MiB is CONTRACT_MISMATCH.
MAX_LINE_BYTES = 1024 * 1024

# OBSERVED prompt delivery enum (run 20261018T111015Z / 111609Z).
DELIVERY_ENUM = ("steer", "queue")
# OBSERVED reply decisions; "always" is never sent (M0-05 policy).
PERMISSION_DECISIONS = ("once", "reject")

# MCP.md §7: approval technical TTL (not a job time limit).
APPROVAL_TTL_SECONDS = 15 * 60


class ContractMismatch(Exception):
    """Framing over 1MiB, malformed UTF-8, or a version/fingerprint pin
    mismatch. Recorded, never dropped, never a success (§5, ch.1)."""

    def __init__(self, reason: str, *, detail: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class ApprovalError(Exception):
    """Approval validation refusal. ``code`` is one of the §7 codes."""

    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# probe — fingerprint + version pin BEFORE any launch (§5)
# ---------------------------------------------------------------------------

def binary_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ProbeResult:
    binary_path: str
    sha256: str
    version: str
    mapping_revision: str


def probe(binary: str, *, version_out: str | None = None,
          sha256_hex: str | None = None) -> ProbeResult:
    """Verify the pinned binary fingerprint and CLI version.

    ``version_out``/``sha256_hex`` are injectable for offline tests; in
    production both are measured. Any mismatch raises ContractMismatch —
    the caller must record it and block execution, demanding M0
    re-verification. No model inference happens here.
    """
    sha = sha256_hex or binary_sha256(binary)
    if sha != PINNED_BINARY_SHA256:
        raise ContractMismatch(
            "binary sha256 does not match the M0 pinned fingerprint",
            detail={"expected": PINNED_BINARY_SHA256, "observed": sha})
    if version_out is None:
        p = subprocess.run([binary, "--version"], capture_output=True,
                           shell=False, check=False)
        version_out = p.stdout.decode("utf-8", "replace").strip()
    if version_out != PINNED_CLI_VERSION:
        raise ContractMismatch(
            "CLI version does not match the M0 pinned version",
            detail={"expected": PINNED_CLI_VERSION, "observed": version_out})
    return ProbeResult(binary_path=os.path.realpath(binary), sha256=sha,
                       version=version_out, mapping_revision=MAPPING_REVISION)


def build_run_env(worktree: str, binary: str) -> dict:
    """ch.2 env prep: PWD pinned to the assigned worktree realpath, CLI
    dir first on PATH. Values are never echoed or logged."""
    worktree = os.path.realpath(worktree)
    env = os.environ.copy()
    env["PWD"] = worktree
    env["PATH"] = os.path.dirname(binary) + os.pathsep + env.get("PATH", "")
    return env


def build_run_argv(binary: str, *, model: str, title: str,
                   prompt: str) -> list[str]:
    """OBSERVED_HELP run argv. ``model`` is caller-supplied and required —
    the adapter never defaults or infers a model. ``--session`` is not
    emitted (continue/create is OPEN_LIVE); follow-ups use the session
    API prompt path."""
    if not model:
        raise ValueError("model must be caller-supplied; never defaulted")
    return [binary, "run", "--model", model, "--format", "json",
            "--title", title, prompt]


# ---------------------------------------------------------------------------
# ch.2 capture — incremental framing/decode, 1MiB cap, CONTRACT_MISMATCH
# ---------------------------------------------------------------------------

class IncrementalLineDecoder:
    """Byte-level line framing with strict per-line UTF-8 decode.

    - Framing is byte-level: a multibyte character split across read
      chunks is safe because decode happens only on complete lines.
    - A line over ``max_line_bytes`` (no newline seen in time) raises
      ContractMismatch — the raw bytes are already streamed to the sink,
      so nothing is dropped, but framing status is CONTRACT_MISMATCH and
      the run can never end as success.
    - Malformed UTF-8 in a complete line raises ContractMismatch.
    - Malformed JSON (valid UTF-8) is NOT a framing failure: the caller
      preserves the line as an unknown/parse-error event (M0-04
      malformed band: "non-JSON line preserved").
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES):
        self.max_line_bytes = max_line_bytes
        self._buf = bytearray()
        self.lines = 0
        self.max_line_seen = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            self._account(line)
            out.append(line)
        if len(self._buf) > self.max_line_bytes:
            self.max_line_seen = len(self._buf)
            raise ContractMismatch(
                f"stdout line exceeds {self.max_line_bytes} bytes",
                detail={"limit_bytes": self.max_line_bytes,
                        "observed_bytes_at_least": len(self._buf),
                        "framing_status": "CONTRACT_MISMATCH"})
        return out

    def finish(self) -> list[bytes]:
        """EOF: a trailing partial line (no final newline) is framed as
        the last line — never silently dropped."""
        if not self._buf:
            return []
        line = bytes(self._buf)
        self._buf.clear()
        self._account(line)
        return [line]

    def _account(self, line: bytes) -> None:
        if len(line) > self.max_line_bytes:
            raise ContractMismatch(
                f"stdout line exceeds {self.max_line_bytes} bytes",
                detail={"limit_bytes": self.max_line_bytes,
                        "observed_bytes": len(line),
                        "framing_status": "CONTRACT_MISMATCH"})
        self.lines += 1
        self.max_line_seen = max(self.max_line_seen, len(line))

    @staticmethod
    def decode(line: bytes) -> str:
        try:
            return line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ContractMismatch(
                "malformed UTF-8 in a framed line",
                detail={"offset": e.start, "framing_status":
                        "CONTRACT_MISMATCH"}) from e


def parse_jsonl_line(line: bytes) -> Any:
    """Decode + json.loads one framed line. Valid UTF-8 that is not JSON
    becomes a preserved sentinel (never dropped, never merged)."""
    text = IncrementalLineDecoder.decode(line)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw_line": text, "parse_error": True}


@dataclass
class CaptureResult:
    exit_code: int | None
    eof: bool
    killed_by_us: bool
    contract_mismatch: bool
    mismatch_reason: str | None
    framing: dict
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    started_at: str
    ended_at: str


def capture_run(binary: str, *, model: str, title: str, prompt: str,
                cwd: str, stdout_sink: BinaryIO, stderr_sink: BinaryIO,
                env: dict | None = None, timeout_s: float | None = None,
                on_line: Callable[[bytes], None] | None = None,
                max_line_bytes: int = MAX_LINE_BYTES) -> CaptureResult:
    """Run ``opencode run --format json`` per the M0 ch.2 algorithm.

    - shell=False, Popen; stdout and stderr drained CONCURRENTLY via
      selectors so a chatty stderr can never deadlock stdout; raw bytes
      go to caller-provided sinks (artifact persistence is P3's scope),
      hashed as they stream; stderr is never merged into stdout.
    - Framing/decode via IncrementalLineDecoder; over-1MiB or malformed
      UTF-8 sets contract_mismatch (raw streaming continues so nothing
      is dropped) and the result can never be judged success.
    - Host timeout kills ONLY our owned child; that is recorded as
      ``killed_by_us`` — never labelled ``cancelled`` (cancel needs
      exact-session stop confirmation).
    - Missing exit code stays None; no fabricated 0.
    """
    argv = build_run_argv(binary, model=model, title=title, prompt=prompt)
    env = env if env is not None else build_run_env(cwd, binary)
    started_at = utcnow()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    decoder = IncrementalLineDecoder(max_line_bytes)
    state = {
        "stdout": {"hash": hashlib.sha256(), "bytes": 0, "eof": False},
        "stderr": {"hash": hashlib.sha256(), "bytes": 0, "eof": False},
    }
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    mismatch: ContractMismatch | None = None
    killed_by_us = False
    timed_out = False  # defined even if the drain loop raises unexpectedly
    deadline = None if timeout_s is None else time.monotonic() + timeout_s

    def drain(name: str, sink: BinaryIO) -> None:
        nonlocal mismatch
        stream = proc.stdout if name == "stdout" else proc.stderr
        chunk = os.read(stream.fileno(), 1 << 16)
        if not chunk:
            state[name]["eof"] = True
            sel.unregister(stream)
            return
        sink.write(chunk)
        st = state[name]
        st["hash"].update(chunk)
        st["bytes"] += len(chunk)
        if name == "stdout" and mismatch is None:
            try:
                for line in decoder.feed(chunk):
                    # UTF-8 validity is ALWAYS checked, even without a
                    # consumer — malformed bytes are CONTRACT_MISMATCH
                    IncrementalLineDecoder.decode(line)
                    if on_line is not None:
                        on_line(line)
            except ContractMismatch as e:
                mismatch = e
        # after a mismatch, raw bytes keep streaming to the sink (never
        # dropped) but framing/parse stops — status stays MISMATCH

    try:
        while sel.get_map():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("host timeout")
                events = sel.select(timeout=remaining)
            else:
                events = sel.select(timeout=30.0)
            for key, _ in events:
                drain(key.data,
                      stdout_sink if key.data == "stdout" else stderr_sink)
    except TimeoutError:
        timed_out = True
        killed_by_us = True
        proc.kill()  # ONLY our owned child — never a shared server
        # drain whatever remains so the child's death is reaped cleanly
        for name, sink in (("stdout", stdout_sink), ("stderr", stderr_sink)):
            try:
                while True:
                    chunk = (proc.stdout if name == "stdout"
                             else proc.stderr).read(1 << 16)
                    if not chunk:
                        break
                    sink.write(chunk)
                    state[name]["hash"].update(chunk)
                    state[name]["bytes"] += len(chunk)
            except (OSError, ValueError):
                pass
        proc.wait()
    finally:
        sel.close()

    if not timed_out:
        for name, sink in (("stdout", stdout_sink),
                           ("stderr", stderr_sink)):
            if not state[name]["eof"]:
                rest = (proc.stdout if name == "stdout"
                        else proc.stderr).read()
                if rest:
                    sink.write(rest)
                    state[name]["hash"].update(rest)
                    state[name]["bytes"] += len(rest)
        if mismatch is None:
            try:
                for line in decoder.finish():
                    if on_line is not None:
                        on_line(line)
            except ContractMismatch as e:
                mismatch = e
        exit_code = proc.wait()
    else:
        exit_code = proc.returncode

    framing = {"lines": decoder.lines, "max_line_bytes_seen":
               decoder.max_line_seen, "limit_bytes": max_line_bytes,
               "framing_status":
                   "CONTRACT_MISMATCH" if mismatch else "OK"}
    return CaptureResult(
        exit_code=exit_code,
        eof=not timed_out and state["stdout"]["eof"] and
            state["stderr"]["eof"],
        killed_by_us=killed_by_us,
        contract_mismatch=mismatch is not None,
        mismatch_reason=str(mismatch) if mismatch else None,
        framing=framing,
        stdout_sha256=state["stdout"]["hash"].hexdigest(),
        stderr_sha256=state["stderr"]["hash"].hexdigest(),
        stdout_bytes=state["stdout"]["bytes"],
        stderr_bytes=state["stderr"]["bytes"],
        started_at=started_at, ended_at=utcnow())


def capture_and_normalize(binary: str, *, attempt_id: str, model: str,
                          title: str, prompt: str, cwd: str,
                          stdout_sink: BinaryIO, stderr_sink: BinaryIO,
                          on_line: Callable[[bytes], None] | None = None,
                          **kw) -> tuple[CaptureResult, EventNormalizer,
                                         list[NormalizedEvent]]:
    """Convenience: capture_run wired to an EventNormalizer. Duplicate
    handling follows contract §3 (identical payload dedupe; same ID with
    different payload kept and marked, never merged).

    ``on_line`` (M4-A3 fix): a caller hook was previously swallowed by
    **kw and then collided with the internal keyword —
    ``capture_run() got multiple values for keyword argument 'on_line'``
    killed every LiveBackend job. It is now a named parameter and the
    caller's hook runs alongside the normalizer for EVERY framed line."""
    norm = EventNormalizer(attempt_id)
    events: list[NormalizedEvent] = []

    def _hook(line: bytes) -> None:
        envelope = parse_jsonl_line(line)
        is_obj = isinstance(envelope, dict) and not envelope.get("parse_error")
        dup = norm.check_dup(envelope) if is_obj else None
        if dup is not None:
            norm.note_duplicate(dup)
            if dup.duplicate:  # same ID + identical payload -> drop
                return
        ev = norm.normalize(envelope, utcnow())
        if dup is not None and dup.conflict:
            ev.payload["dup_conflict"] = True  # same ID, different payload
        events.append(ev)
        if on_line is not None:
            on_line(line)

    result = capture_run(binary, model=model, title=title, prompt=prompt,
                         cwd=cwd, stdout_sink=stdout_sink,
                         stderr_sink=stderr_sink, on_line=_hook, **kw)
    return result, norm, events


# ---------------------------------------------------------------------------
# session API transport — OBSERVED argv shape (run_m005/006/008)
# ---------------------------------------------------------------------------

@dataclass
class ApiResponse:
    method: str
    path: str
    exit_code: int
    body: Any
    raw_stdout: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def data(self) -> Any:
        if isinstance(self.body, dict):
            return self.body.get("data")
        return None


class SessionApiTransport:
    """``opencode api --server <url> METHOD path [-d json]``.

    Credentials: the server password is read ONLY from the
    OPENCODE_PASSWORD environment variable at call time — never argv,
    never stored on the instance, never logged (the observed runners set
    it in the child env exactly this way). ``process_runner`` is
    injectable for offline tests: callable(argv, env) ->
    (exit_code, stdout_text, stderr_text).
    """

    def __init__(self, binary: str, server_url: str, *,
                 process_runner: Callable[..., tuple[int, str, str]] | None = None):
        self.binary = binary
        self.server_url = server_url
        self._runner = process_runner

    def call(self, method: str, path: str,
             data: dict | None = None) -> ApiResponse:
        argv = [self.binary, "api", "--server", self.server_url,
                method, path]
        if data is not None:
            argv += ["-d", json.dumps(data, ensure_ascii=False)]
        env = os.environ.copy()  # OPENCODE_PASSWORD lives here only
        if self._runner is not None:
            code, out, err = self._runner(argv, env)
        else:
            p = subprocess.run(argv, env=env, capture_output=True,
                               shell=False, check=False)
            code = p.returncode
            out = p.stdout.decode("utf-8", "replace")
            err = p.stderr.decode("utf-8", "replace")
        try:
            body = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            body = None
        return ApiResponse(method=method, path=path, exit_code=code,
                           body=body, raw_stdout=out)


# ---------------------------------------------------------------------------
# adapter — session/turn identity, permission, interrupt, reconcile
# ---------------------------------------------------------------------------

@dataclass
class CancelResult:
    confirmed: bool
    reason: str
    ack_exit_code: int | None = None


class OpenCodeV2Adapter:
    """Session-scoped operations against an owned server URL.

    Every method targets ONE exact session id; nothing here searches by
    title, kills a shared server, or touches other sessions. Cancel is
    never confirmed by the HTTP ack alone (§4).
    """

    def __init__(self, transport: SessionApiTransport):
        self.transport = transport

    def create_session(self, *, title: str, model: dict, agent: str = "build",
                       permissions: list | None = None) -> str:
        """OBSERVED: POST /api/session -> body.data.id (ses_…).
        ``model`` is caller-supplied ({providerID, id}); never defaulted."""
        if not isinstance(model, dict) or not model.get("id") \
                or not model.get("providerID"):
            raise ValueError("model must be {providerID, id}, caller-supplied")
        body: dict[str, Any] = {"title": title, "agent": agent,
                                "model": model}
        if permissions is not None:
            body["permissions"] = permissions
        r = self.transport.call("POST", "/api/session", body)
        if not r.ok or not isinstance(r.data, dict) or not r.data.get("id"):
            raise ContractMismatch("session create failed",
                                   detail={"exit_code": r.exit_code})
        return r.data["id"]

    def prompt(self, session_id: str, text: str,
               delivery: str | None = None) -> ApiResponse:
        """POST /api/session/{sid}/prompt. Busy follow-up is NEVER
        refused — there is no reject branch and no busy pre-check; the
        observed backend accepted every prompt with exit 0. ``delivery``
        must come from the observed enum [steer, queue]; steer injects
        into the running turn, queue starts an independent new turn."""
        if delivery is not None and delivery not in DELIVERY_ENUM:
            raise ValueError(f"delivery must be one of {DELIVERY_ENUM}")
        body: dict[str, Any] = {"text": text}
        if delivery is not None:
            body["delivery"] = delivery
        return self.transport.call(
            "POST", f"/api/session/{session_id}/prompt", body)

    def pending_permissions(self, session_id: str) -> list[dict]:
        r = self.transport.call(
            "GET", f"/api/session/{session_id}/permission")
        data = r.data
        return data if isinstance(data, list) else []

    def reply_permission(self, session_id: str, request_id: str,
                         decision: str) -> ApiResponse:
        """OBSERVED reply API. Only once/reject — 'always' is never
        sent, and no global permission config is modified."""
        if decision not in PERMISSION_DECISIONS:
            raise ValueError(
                f"decision must be one of {PERMISSION_DECISIONS}; "
                "'always' is never sent")
        return self.transport.call(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            {"decision": decision})

    def messages(self, session_id: str) -> ApiResponse:
        return self.transport.call("GET",
                                   f"/api/session/{session_id}/message")

    def active_sessions(self) -> list[str]:
        """GET /api/session/active — body.data keyed by RUNNING session
        ids (observed shape)."""
        r = self.transport.call("GET", "/api/session/active")
        data = r.data
        if isinstance(data, dict):
            return sorted(data.keys())
        if isinstance(data, list):
            return sorted(x.get("sessionID") or x.get("id")
                          for x in data if isinstance(x, dict))
        return []

    def interrupt(self, session_id: str) -> ApiResponse:
        """POST .../interrupt (no body). The ``{"interrupted": true}``
        response is an ACK only — never a cancelled verdict."""
        return self.transport.call(
            "POST", f"/api/session/{session_id}/interrupt")

    def confirm_cancel(self, session_id: str, *, ack: ApiResponse,
                       observer: Callable[[str], list[str]] | None = None,
                       grace_s: float = 0.0,
                       sleep: Callable[[float], None] = time.sleep,
                       deadline_s: float = 10.0) -> CancelResult:
        """Confirm stop by OBSERVING the exact session's activity.

        The ack's exit code is recorded but never sufficient. The
        session must disappear from the active set within the deadline;
        if it is still active, the result is NOT confirmed and the
        caller must treat it as needs_attention (workspace lease kept).
        ``observer`` is injectable so tests can simulate other sessions
        surviving; the default is GET /api/session/active.
        """
        if ack.exit_code != 0:
            return CancelResult(False, "interrupt ack failed",
                                ack_exit_code=ack.exit_code)
        observe = observer or (lambda sid: self.active_sessions())
        if grace_s > 0:
            sleep(grace_s)
        limit = time.monotonic() + deadline_s
        while True:
            active = observe(session_id)
            if session_id not in active:
                return CancelResult(True, "exact session absent from "
                                    "active set", ack_exit_code=ack.exit_code)
            if time.monotonic() >= limit:
                return CancelResult(False,
                                    "CANCEL_NOT_CONFIRMED: session still "
                                    "active after interrupt ack",
                                    ack_exit_code=ack.exit_code)
            sleep(0.2)

    def reconcile(self, session_id: str) -> str:
        """running / pending / idle / missing / unknown (contract §3).
        idle alone is NEVER completion — completion is judge_completion
        over this attempt's events."""
        try:
            if session_id in self.active_sessions():
                return "running"
            if self.pending_permissions(session_id):
                return "pending"
            r = self.transport.call("GET", f"/api/session/{session_id}")
            if r.ok and isinstance(r.data, dict) and r.data.get("id"):
                return "idle"
            if not r.ok:
                return "missing"
            return "unknown"
        except ContractMismatch:
            return "unknown"


# ---------------------------------------------------------------------------
# approval grants — MCP.md §7 (challenge, TTL, replay-first, channel)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: str
    attempt_id: str
    backend_request_id: str
    principal: str
    scope_sha256: str
    generation: int
    expires_at: float          # epoch seconds; TTL is 15 min from issue
    channel_verified: bool
    decision: str              # once | reject
    challenge: str | None      # base64url HMAC; None when channel unverified

    @property
    def challenge_sha256(self) -> str | None:
        """What the DB may store — the challenge value itself must never
        be persisted or logged (§7)."""
        if self.challenge is None:
            return None
        return hashlib.sha256(self.challenge.encode("utf-8")).hexdigest()


def canonical_challenge_payload(grant: ApprovalGrant) -> str:
    return canonical_json({
        "principal": grant.principal,
        "approval_id": grant.approval_id,
        "attempt_id": grant.attempt_id,
        "backend_request_id": grant.backend_request_id,
        "scope_sha256": grant.scope_sha256,
        "generation": grant.generation,
        "expires_at": grant.expires_at,
    })


def compute_challenge(key: bytes, grant: ApprovalGrant) -> str:
    """base64url( HMAC-SHA256(key, canonical payload) ), unpadded —
    §7: 32-byte HMAC, base64url."""
    digest = hmac.new(key, canonical_challenge_payload(grant).encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class ApprovalJournal:
    """Replay journal: an already-accepted approval request is answered
    from the journal BEFORE any TTL/state recheck (§7)."""

    def __init__(self) -> None:
        self._accepted: dict[str, dict] = {}

    def lookup(self, approval_id: str, attempt_id: str,
               backend_request_id: str) -> dict | None:
        rec = self._accepted.get(approval_id)
        if rec and rec["attempt_id"] == attempt_id \
                and rec["backend_request_id"] == backend_request_id:
            return rec
        return None

    def record(self, approval_id: str, attempt_id: str,
               backend_request_id: str, result: dict) -> None:
        self._accepted[approval_id] = {
            "attempt_id": attempt_id,
            "backend_request_id": backend_request_id,
            "result": result, "recorded_at": utcnow()}


def validate_and_reply(
    adapter: OpenCodeV2Adapter,
    session_id: str,
    grant: ApprovalGrant,
    *,
    key: bytes,
    current_attempt_id: str,
    journal: ApprovalJournal,
    now: float | None = None,
    pending: list[dict] | None = None,
) -> ApiResponse:
    """Validate an ApprovalGrant and, only if every check passes, send
    the backend reply. Check order is pinned by §7:

    1. replay journal FIRST — an already-accepted request returns its
       recorded result without re-checking TTL/state or re-executing;
    2. channel: unverified UI channel -> APPROVAL_CHANNEL_UNAVAILABLE
       (blocks execution; challenge is null in that state);
    3. TTL: past ``expires_at`` -> APPROVAL_EXPIRED (job goes to
       needs_attention upstream; pending state is re-checked there);
    4. attempt: grant bound to a different attempt -> STALE_ATTEMPT;
    5. challenge: recomputed with the key and compared with
       hmac.compare_digest -> APPROVAL_INVALID on any mismatch/forgery;
    6. scope: the backend pending request must still exist and its
       recomputed scope_sha256 must match the grant — a changed scope is
       a NEW confirmation, never silently approved;
    7. decision: only once/reject.
    """
    # 1. replay first
    rec = journal.lookup(grant.approval_id, grant.attempt_id,
                         grant.backend_request_id)
    if rec is not None:
        r = rec["result"]
        return ApiResponse(method="REPLAY", path=f"/approval/{grant.approval_id}",
                           exit_code=r.get("exit_code", 0),
                           body={"replayed": True, "recorded_at":
                                 rec["recorded_at"]}, raw_stdout="")

    # 2. channel
    if not grant.channel_verified:
        raise ApprovalError("APPROVAL_CHANNEL_UNAVAILABLE",
                            {"approval_id": grant.approval_id})
    # 3. TTL
    if (now if now is not None else time.time()) > grant.expires_at:
        raise ApprovalError("APPROVAL_EXPIRED",
                            {"approval_id": grant.approval_id,
                             "expires_at": grant.expires_at})
    # 4. attempt binding
    if grant.attempt_id != current_attempt_id:
        raise ApprovalError("STALE_ATTEMPT",
                            {"grant_attempt": grant.attempt_id,
                             "current_attempt": current_attempt_id})
    # 5. challenge authenticity (constant-time compare; forgery -> invalid)
    if grant.challenge is None:
        raise ApprovalError("APPROVAL_INVALID",
                            {"reason": "channel verified but challenge null"})
    expected = compute_challenge(key, grant)
    if not hmac.compare_digest(expected, grant.challenge):
        raise ApprovalError("APPROVAL_INVALID",
                            {"reason": "challenge does not verify",
                             "approval_id": grant.approval_id})
    # 6. scope binding against the live pending request
    if pending is None:
        pending = adapter.pending_permissions(session_id)
    req = next((p for p in pending
                if p.get("id") == grant.backend_request_id), None)
    if req is None:
        raise ApprovalError("APPROVAL_INVALID",
                            {"reason": "backend request no longer pending",
                             "backend_request_id": grant.backend_request_id})
    live_scope = scope_sha256(req)
    if not hmac.compare_digest(live_scope, grant.scope_sha256):
        raise ApprovalError("APPROVAL_INVALID",
                            {"reason": "scope drift: new scope requires a "
                                       "new user confirmation",
                             "backend_request_id": grant.backend_request_id})
    # 7. decision
    if grant.decision not in PERMISSION_DECISIONS:
        raise ApprovalError("APPROVAL_INVALID",
                            {"reason": f"decision must be one of "
                                       f"{PERMISSION_DECISIONS}"})

    resp = adapter.reply_permission(session_id, grant.backend_request_id,
                                    grant.decision)
    if resp.ok:
        journal.record(grant.approval_id, grant.attempt_id,
                       grant.backend_request_id,
                       {"exit_code": resp.exit_code})
    return resp
