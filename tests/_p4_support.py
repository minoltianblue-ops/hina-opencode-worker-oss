"""P4 test support: scriptable FakeBackend, supervisor harness, raw-UDS
and stdio-proxy clients, and a JSON-Schema-subset output validator.

Reuses P3's git fixtures (_wt_support) unchanged. Everything lives under
one temp dir per test; teardown removes it and kills spawned proxies, so
residuals stay at zero.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _wt_support import ROOT, SCHEMA_SQL, git, make_repo, write  # noqa: F401

SRC = ROOT / "src"
TOOLS_JSON = ROOT / "docs" / "contracts" / "mcp-tools.json"

sys.path.insert(0, str(SRC)) if str(SRC) not in sys.path else None

from hina_opencode_worker import supervisor as sup  # noqa: E402
from hina_opencode_worker.supervisor import (  # noqa: E402
    AttemptOutcome, BackendRun, BackendTimeout, BackendUnavailable,
    CancelResult, ContractMismatch, ModelCatalog, ModelUnavailableError,
    Supervisor, SupervisorConfig, SupervisorBackend)
from hina_opencode_worker.events import (  # noqa: E402
    VERDICT_COMPLETED, VERDICT_CLI_FAILED)

REPO_ID_RE = re.compile(r"^job_[A-Za-z0-9]+$")


def fake_job_id() -> str:
    import uuid
    return "job_" + str(uuid.uuid4())


def fake_att_id() -> str:
    import uuid
    return "att_" + str(uuid.uuid4())


def rid() -> str:
    """Contract request_id pattern is req_<lowercase uuid4>."""
    import uuid
    return "req_" + str(uuid.uuid4())


class FakeBackend(SupervisorBackend):
    """Scriptable per-run behaviour.

    run_scripts: dict session-will-be-assigned -> list of steps consumed
    one per wait() call. A step is:
      None                 -> keep observing (returns after deadline)
      AttemptOutcome       -> terminal verdict
      ("permission", req)  -> on_permission(req), keep observing
      ("sleep", secs)      -> burn wall time, keep observing
    start_error / probe_error / cancel_confirmed / reconcile_result /
    slow_reconcile_s control the other seams.
    """

    def __init__(self):
        self.unavailable: set[str] = set()
        self.start_error: Exception | None = None
        self.probe_error: Exception | None = None
        self.cancel_confirmed = True
        self.reconcile_result = "idle"
        self.reconcile_error: Exception | None = None
        self.slow_reconcile_s = 0.0
        self.steps: list = []              # steps for the NEXT start
        self.runs: dict[str, list] = {}    # session_id -> remaining steps
        self.started: list[dict] = []
        self.reconcile_calls: list[float] = []
        self.reply_log: list[tuple] = []
        self.pending: list[dict] = []
        self._n = 0
        self._lock = threading.Lock()

    # -- contract surface ------------------------------------------------
    def probe(self) -> dict:
        if self.probe_error is not None:
            raise self.probe_error
        return {"version": "fake-1.0", "binary_sha256": "f" * 64,
                "mapping_revision": "p4-fake"}

    def available(self, model: str) -> bool:
        return model not in self.unavailable

    def start(self, *, job_id, attempt_id, model, cwd, prompt) -> BackendRun:
        if self.start_error is not None:
            err, self.start_error = self.start_error, None
            raise err
        with self._lock:
            self._n += 1
            sid = f"ses_fake{self._n:04d}"
            self.runs[sid] = list(self.steps)
        self.started.append({"job_id": job_id, "attempt_id": attempt_id,
                             "model": model, "cwd": cwd, "prompt": prompt,
                             "session_id": sid})
        return BackendRun(sid, handle={"cwd": cwd})

    def wait(self, run, *, cancel, deadline_s, on_permission):
        steps = self.runs.setdefault(run.session_id, [])
        if steps:
            step = steps.pop(0)
            if isinstance(step, tuple) and step[0] == "permission":
                self.pending.append(step[1])
                on_permission(step[1])
                return None
            if isinstance(step, tuple) and step[0] == "sleep":
                end = time.monotonic() + min(step[1], deadline_s)
                while time.monotonic() < end and not cancel.is_set():
                    time.sleep(0.01)
                return None
            if isinstance(step, tuple) and step[0] == "hang":
                # slow backend: burn the WHOLE poll slice every call —
                # worker_wait must still return at its own budget.
                end = time.monotonic() + deadline_s
                while time.monotonic() < end and not cancel.is_set():
                    time.sleep(0.02)
                return None
            return step
        # idle: honour the slice, never longer, never holding a lock
        end = time.monotonic() + deadline_s
        while time.monotonic() < end and not cancel.is_set():
            time.sleep(0.02)
        return None

    def reconcile(self, session_id: str, timeout_s: float) -> str:
        self.reconcile_calls.append(timeout_s)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        if self.slow_reconcile_s:
            time.sleep(self.slow_reconcile_s)
        return self.reconcile_result

    def pending_permissions(self, session_id):
        return list(self.pending)

    def reply_permission(self, session_id, request_id, decision) -> bool:
        self.reply_log.append((session_id, request_id, decision))
        self.pending = [p for p in self.pending
                        if p.get("id") != request_id]
        return True

    def cancel(self, run) -> CancelResult:
        self.runs.setdefault(run.session_id, []).clear()
        return CancelResult(self.cancel_confirmed,
                            "fake ack" if self.cancel_confirmed
                            else "fake: no session stop ack")


def outcome_completed(session_id=None, usage="unknown"):
    return AttemptOutcome(VERDICT_COMPLETED, "EOF + exit 0 + final message",
                          usage=usage, session_id=session_id,
                          raw_stdout=b'{"type":"text"}\n', exit_code=0)


def outcome_failed(reason="CLI exit 1", session_id="ses_fake0001"):
    return AttemptOutcome(VERDICT_CLI_FAILED, reason,
                          session_id=session_id, exit_code=1)


def outcome_permission_then_done(request, session_id="ses_fake0001"):
    return [("permission", request),
            AttemptOutcome(VERDICT_COMPLETED, "after approval",
                           session_id=session_id, raw_stdout=b"x",
                           exit_code=0)]


class Clock:
    """Real-advancing clocks with a shared injected offset (cursor TTL,
    approval TTL). advance() jumps BOTH timelines forward; time never
    runs backwards or stalls, so wait/deadline loops still terminate."""

    def __init__(self):
        self.wall_offset = 0.0
        self.mono_offset = 0.0

    def time(self):
        return time.time() + self.wall_offset

    def monotonic(self):
        return time.monotonic() + self.mono_offset

    def advance(self, secs):
        self.wall_offset += secs
        self.mono_offset += secs


class P4TestCase(unittest.TestCase):
    """Supervisor on a real temp git repo + scriptable backend.

    UDS is started per-test; `raw()` speaks the wire protocol directly,
    `proxy()` is a real stdio subprocess (T15/T16).
    """

    develop_verified = True
    approval_channel_verified = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p4_"))
        self.repo = make_repo(self.tmp / "repo")
        self.root = self.tmp / "sup"
        self.backend = FakeBackend()
        self.catalog = ModelCatalog(profiles=("inspect", "develop"),
                                    gate="PASS",
                                    capabilities={"develop": True})
        self.catalog.add("fake/free", "free", probe_status="live_verified")
        self.catalog.add("fake/go", "go", probe_status="live_verified")
        self.catalog.add("fake/unknown", "unknown",
                              probe_status="live_verified")
        self.catalog.add("fake/off", "free", probe_status="unavailable")
        self.catalog.add("fake/dis", "go", enabled=False,
                              probe_status="live_verified")
        self.clock = Clock()
        self.config = SupervisorConfig(
            root=str(self.root),
            develop_enforcement_verified=self.develop_verified,
            approval_channel_verified=self.approval_channel_verified,
            wait_poll_s=0.05)
        self.sup = Supervisor(
            self.config, backend=self.backend, catalog=self.catalog,
            clock=self.clock.monotonic, time_fn=self.clock.time)
        self.sock_path = self.sup.start_uds()
        self._proxies: list[subprocess.Popen] = []

    def tearDown(self):
        for proc in self._proxies:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self.sup.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        left = [p for p in Path(self.tmp).parent.glob("p4_*")
                if p == self.tmp]
        self.assertFalse(left, "temp root survived teardown")

    # ------------------------------------------------------------- helpers

    def call(self, tool, args, *, principal="local:%d" % os.getuid()):
        return self.sup.handle(tool, args, principal=principal)

    def uid_principal(self):
        return f"local:{os.getuid()}"

    def start_args(self, request_id=None, *, model="fake/free",
                   profile="inspect", goal="inspect the readme",
                   repo=None, **over):
        a = {
             "request_id": request_id or rid(),
             "user_request_ref": "req/2026/0001",
             "goal": goal,
             "workspace": {"repository": str(repo or self.repo),
                           "source_ref": "HEAD"},
             "profile": profile, "model": model,
             "constraints": [], "done_when": ["answer given"]}
        a.update(over)
        return a

    def raw(self, req: dict, *, path=None) -> dict:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(15.0)
        try:
            conn.connect(path or self.sock_path)
            payload = json.dumps(req).encode("utf-8")
            conn.sendall(struct.pack(">I", len(payload)) + payload)
            hdr = self._recv(conn, 4)
            (length,) = struct.unpack(">I", hdr)
            body = self._recv(conn, length)
            return json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    @staticmethod
    def _recv(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise AssertionError("short UDS read")
            buf += chunk
        return buf

    def proxy(self) -> "ProxyClient":
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
        proc = subprocess.Popen(
            [sys.executable, "-m", "hina_opencode_worker.mcp_proxy",
             "--socket", self.sock_path, "--tools", str(TOOLS_JSON),
             "--verbose"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self._proxies.append(proc)
        return ProxyClient(proc)

    def wait_for(self, job_id, states, timeout=10.0):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            conn = self.sup._read()
            try:
                row = conn.execute(
                    "SELECT state FROM jobs WHERE job_id = ?",
                    (job_id,)).fetchone()
            finally:
                conn.close()
            last = row["state"] if row else None
            if last in states:
                return last
            time.sleep(0.03)
        conn = self.sup._read()
        try:
            evs = [dict(r) for r in conn.execute(
                "SELECT from_state,to_state,reason_code,observed_at FROM"
                " state_events WHERE job_id = ? ORDER BY seq",
                (job_id,))]
            reqs = [dict(r) for r in conn.execute(
                "SELECT request_id,tool,phase,attempt_id FROM requests"
                " WHERE job_id = ?", (job_id,))]
            ats = [dict(r) for r in conn.execute(
                "SELECT attempt_id,ordinal,state,model FROM attempts"
                " WHERE job_id = ? ORDER BY ordinal", (job_id,))]
        finally:
            conn.close()
        raise AssertionError(
            f"job {job_id} stuck in {last!r}, wanted {states}"
            + f"\nevents={evs}"
            + f"\nrequests={reqs}"
            + f"\nattempts={ats}")

    def job_state(self, job_id):
        conn = self.sup._read()
        try:
            row = conn.execute("SELECT state FROM jobs WHERE job_id = ?",
                               (job_id,)).fetchone()
            return row["state"] if row else None
        finally:
            conn.close()

    def finish(self, jid, timeout=8.0):
        """Cancel a running/parked job and wait for a terminal state so
        no runner thread outlives the test (residual 0)."""
        for _ in range(3):
            st = self.job_state(jid)
            if st in ("cancelled", "failed", "completed"):
                return st
            att = self.sup._job_row(jid)["active_attempt_id"]
            self.call("worker_cancel",
                      {"request_id": rid(), "job_id": jid,
                       "expected_attempt_id": att})
            try:
                return self.wait_for(
                    jid, ("cancelled", "failed", "completed"),
                    timeout=timeout)
            except AssertionError:
                continue
        return self.job_state(jid)

    def completed_start(self, request_id=None, **over):
        """start -> job running -> COMPLETED quickly."""
        self.backend.steps = [outcome_completed()]
        env = self.call("worker_start",
                        self.start_args(request_id, **over))
        self.assertIn("data", env, f"start failed: {env}")
        job_id = env["data"]["job"]["job_id"]
        self.wait_for(job_id, ("completed",))
        return job_id


class ProxyClient:
    """Line-delimited JSON-RPC against the proxy subprocess."""

    def __init__(self, proc):
        self.proc = proc
        self._id = 0

    def rpc(self, method, params=None, *, id=True):
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if id:
            msg["id"] = self._id
        if params is not None:
            msg["params"] = params
        self.send_raw(msg)
        if not id:
            return None
        return self.recv()

    def send_raw(self, msg):
        line = json.dumps(msg, ensure_ascii=False)
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def send_bytes(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv(self, timeout=15.0):
        line = self._readline(timeout)
        return json.loads(line.decode("utf-8"))

    def _readline(self, timeout=15.0):
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise AssertionError("proxy stdout timeout")
            ch = self.proc.stdout.read(1)
            if not ch:
                raise AssertionError(f"proxy stdout EOF (stderr="
                                     f"{self.proc.stderr.read(2048)!r})")
            if ch == b"\n":
                return buf
            buf += ch

    def initialize(self):
        return self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "t",
                                                      "version": "0"}})

    def call_tool(self, name, arguments):
        return self.rpc("tools/call", {"name": name,
                                       "arguments": arguments})


# --------------------------------------------------------------------------
# JSON-Schema subset validator (the contract uses only this subset)
# --------------------------------------------------------------------------

def validate_against(node, value, path="$"):
    """Returns a list of violation strings ([] = valid)."""
    errs = []
    if not isinstance(node, dict):
        return errs
    t = node.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            errs.append(f"{path}: expected {types}, got "
                        f"{type(value).__name__}")
            return errs
    if "const" in node and value != node["const"]:
        errs.append(f"{path}: != const {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errs.append(f"{path}: shorter than {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errs.append(f"{path}: longer than {node['maxLength']}")
        if "pattern" in node and not re.search(node["pattern"], value):
            errs.append(f"{path}: pattern {node['pattern']!r} violated")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errs.append(f"{path}: < minimum {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errs.append(f"{path}: > maximum {node['maximum']}")
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errs.append(f"{path}: fewer than {node['minItems']} items")
        if "maxItems" in node and len(value) > node["maxItems"]:
            errs.append(f"{path}: more than {node['maxItems']} items")
        item_node = node.get("items")
        if isinstance(item_node, dict):
            for i, x in enumerate(value):
                errs += validate_against(item_node, x, f"{path}[{i}]")
    if isinstance(value, dict):
        props = node.get("properties") or {}
        for req in node.get("required") or ():
            if req not in value:
                errs.append(f"{path}: missing required {req!r}")
        if node.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{path}: additional property {k!r}")
        for k, v in value.items():
            if k in props:
                errs += validate_against(props[k], v, f"{path}.{k}")
            elif isinstance(node.get("additionalProperties"), dict):
                errs += validate_against(node["additionalProperties"], v,
                                         f"{path}.{k}")
    for combinator in ("anyOf", "oneOf"):
        if combinator in node:
            ok_children = [c for c in node[combinator]
                           if not validate_against(c, value, path)]
            if combinator == "anyOf" and not ok_children:
                errs.append(f"{path}: no anyOf branch matched")
            if combinator == "oneOf" and len(ok_children) != 1:
                errs.append(f"{path}: oneOf matched "
                            f"{len(ok_children)} branches")
    if "allOf" in node:
        for c in node["allOf"]:
            errs += validate_against(c, value, path)
    return errs


def _type_ok(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(
            value, bool)
    if t == "null":
        return value is None
    return True


def tools_doc():
    return json.loads(TOOLS_JSON.read_text())


def output_schema(tool):
    return {t["name"]: t["outputSchema"] for t in tools_doc()["tools"]}[tool]


def assert_envelope(tc: unittest.TestCase, env: dict, tool: str, *,
                    check_schema=True):
    """Envelope + data must conform to the frozen outputSchema."""
    tc.assertIsInstance(env, dict)
    tc.assertEqual(env.get("contract_version"), "0.3", "contract version")
    tc.assertEqual(env.get("tool"), tool)
    tc.assertIn("observed_at", env)
    tc.assertTrue(("data" in env) ^ ("error" in env),
                  f"exactly one of data/error: {env}")
    rec = env.get("receipt")
    tc.assertIsInstance(rec, dict, "receipt present")
    if check_schema and "data" in env:
        errs = validate_against(output_schema(tool), env, "$")
        tc.assertEqual(errs, [], f"{tool} output schema violations")
