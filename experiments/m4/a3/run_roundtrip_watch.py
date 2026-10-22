#!/usr/bin/env python3
"""M4-A3 round-trip watcher — verify the real ChatGPT round trip AFTER
the user registered the connector and sent the agreed test prompt.

Observation is read-only everywhere:
  viewer.snapshot      (observe-only UDS client; never mutates)
  worker_status /      endpoint->proxy->UDS contract reads (read-only
  worker_result        tools per MCP.md §3)
  sqlite (mode=ro)     journal truth: jobs/requests/attempts/
                       state_events/backend_events/artifacts
  opencode api GET     session existence on the resident server
                       (session identity cross-check, no writes)
  endpoint.err.log     tunnel-hop connection log (connector -> endpoint)

Verifications (brief step 3):
  completion           job reaches a terminal state (completed expected)
  same session/turn    attempts.session_id vs backend_events/raw-artifact
                       sessionIDs vs the server's view — every source
                       recorded; equalities reported as booleans, never
                       smoothed over (a mismatch is a documented finding)
  result preserved     worker_result retrievable after completion; a
                       second read is identical; artifact sha256 matches
                       the registry
  no auto next job     exactly 1 job / 1 request in the journal; no
                       followup/cancel/start rows; no further
                       state_events after terminal (two observations)
Exit 0 = round trip completed and all hard checks pass; 2 = completed
with documented findings; 1 = failed/timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import a3_stack  # noqa: E402

TERMINAL = ("completed", "failed", "cancelled", "needs_attention",
            "waiting_model_selection")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def log(msg: str) -> None:
    print("[%s] %s" % (now(), msg), flush=True)


def snapshot(sock: str) -> dict:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(10.0)
    try:
        conn.connect(sock)
        payload = json.dumps({"client": "viewer",
                              "method": "viewer.snapshot",
                              "arguments": {}}).encode()
        conn.sendall(struct.pack(">I", len(payload)) + payload)
        hdr = conn.recv(4)
        (n,) = struct.unpack(">I", hdr)
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("short read")
            buf += chunk
        return json.loads(buf)
    finally:
        conn.close()


def endpoint_call(host: str, port: int, tool: str, args: dict) -> dict:
    """tools/call via the tunnel endpoint (ChatGPT's own entry point)."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "m4a3-watch", "version": "1"}}}
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    conn = socket.create_connection((host, port), timeout=30)
    try:
        for m in (init, notif, call):
            conn.sendall((json.dumps(m) + "\n").encode())
        want = {1, 2}
        out: dict[int, dict] = {}
        buf = b""
        deadline = time.monotonic() + 30
        while want and time.monotonic() < deadline:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                d = json.loads(line.decode())
                if d.get("id") in want:
                    want.discard(d["id"])
                    out[d["id"]] = d
        if 2 not in out:
            return {"error": "no tools/call reply"}
        return out[2].get("result", {}).get("structuredContent") \
            or out[2].get("error") or {}
    finally:
        conn.close()


def ro_store(db: str) -> sqlite3.Connection:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def rows(c: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=a3_stack.DEFAULT_ROOT)
    ap.add_argument("--job-timeout", type=float, default=3000.0,
                    help="seconds to wait for the job to APPEAR "
                         "(user-driven; long by design — not a model job "
                         "timeout)")
    ap.add_argument("--settle-observe-s", type=float, default=15.0,
                    help="quiet window after terminal state")
    ap.add_argument("--job-id", default=None,
                    help="post-hoc mode: verify an EXISTING job (e.g. the"
                         " attempt-1 defect evidence); skips the wait"
                         " loops, journal is the source of truth")
    ap.add_argument("--expect-answer", default=None,
                    help="delivery check: the agreed answer token (e.g. "
                         "CODE=HINA-A3-92483418) must appear VERBATIM in "
                         "the worker_result summary the connector reads; "
                         "without it the check still requires an "
                         "'answer:' block")
    ns = ap.parse_args(argv)
    a3_stack.ensure_opencode_password_env()
    paths = a3_stack.root_paths(os.path.abspath(
        os.path.expanduser(ns.root)))
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    db = os.path.join(paths["root"], "db", "worker.db")
    sock = paths["sock"]

    baseline_jobs: set[str] = set()
    t0 = time.monotonic()
    job_id = None
    seen_states: list[tuple[str, str]] = []
    job_state = None
    terminal_seq = None
    if ns.job_id:
        # ---- post-hoc mode: verify an existing job from the journal ----
        job_id = ns.job_id
        _c = ro_store(db)
        try:
            jr = _c.execute("SELECT state FROM jobs WHERE job_id=?",
                            (job_id,)).fetchone()
            job_state = jr["state"]
            seen_states = [(r["observed_at"], r["to_state"]) for r in rows(
                _c, "SELECT observed_at,to_state FROM state_events"
                    " WHERE job_id=? ORDER BY seq", (job_id,))]
            baseline_jobs = {r["job_id"] for r in rows(_c, "SELECT job_id"
                            " FROM jobs") if r["job_id"] != job_id}
            terminal_seq = (_c.execute(
                "SELECT MAX(seq) FROM state_events WHERE job_id=?",
                (job_id,)).fetchone()[0], len(_c.execute(
                "SELECT 1 FROM jobs").fetchall()))
        finally:
            _c.close()
        log("post-hoc mode: job %s state=%s from journal" % (job_id,
                                                             job_state))
    else:
        # ---- A: wait for the NEW job to appear (ChatGPT sends
        # worker_start). Baseline jobs (a previous attempt at this same
        # root, e.g. my live re-verification job) are recorded but never
        # counted as "the" round trip nor against no-auto-next-job.
        log("watching for the ChatGPT worker_start (timeout %ss)..."
            % ns.job_timeout)
        try:
            baseline_jobs = {j["job_id"]
                             for j in (snapshot(sock).get("data") or {}).get(
                                 "jobs") or []}
        except OSError:
            pass
        if baseline_jobs:
            log("baseline (pre-existing) jobs recorded, NOT the round"
                " trip: %s" % sorted(baseline_jobs))
    while (not ns.job_id
           and time.monotonic() - t0 < ns.job_timeout):
        try:
            snap = snapshot(sock)
            jobs = (snap.get("data") or {}).get("jobs") or []
        except OSError as e:
            log("snapshot error: %s" % e)
            time.sleep(2)
            continue
        new = [j for j in jobs if j["job_id"] not in baseline_jobs]
        if new:
            job = new[0]
            job_id = job["job_id"]
            if job_state != job["state"]:
                job_state = job["state"]
                seen_states.append((now(), job_state))
                log("job %s state=%s" % (job_id, job_state))
            if job_state in TERMINAL:
                try:
                    _c = ro_store(db)
                    terminal_seq = (_c.execute(
                        "SELECT MAX(seq) FROM state_events WHERE job_id=?",
                        (job_id,)).fetchone()[0], len(_c.execute(
                        "SELECT 1 FROM jobs").fetchall()))
                    _c.close()
                except sqlite3.Error:
                    pass
                break
        time.sleep(2)
    if job_id is None:
        _dump(out, "roundtrip-timeout.json",
              {"ts": now(), "reason": "no job appeared within"
                                      " --job-timeout"})
        log("TIMEOUT: no job appeared — user prompt may not have been"
            " sent/registered yet")
        return 1

    # ---- B: follow to terminal ----
    deadline = time.monotonic() + ns.job_timeout
    while job_state not in TERMINAL and time.monotonic() < deadline:
        try:
            snap = snapshot(sock)
            jobs = (snap.get("data") or {}).get("jobs") or []
            st = next((j["state"] for j in jobs
                       if j["job_id"] == job_id), job_state)
            if st != job_state:
                job_state = st
                seen_states.append((now(), job_state))
                log("state=%s" % st)
        except OSError as e:
            log("snapshot error: %s" % e)
        time.sleep(2)
    log("terminal-ish state=%s" % job_state)

    # settle window: no new state_events after terminal (no auto next job)
    if terminal_seq is None:
        try:
            _c = ro_store(db)
            terminal_seq = (_c.execute(
                "SELECT MAX(seq) FROM state_events WHERE job_id=?",
                (job_id,)).fetchone()[0], len(_c.execute(
                "SELECT 1 FROM jobs").fetchall()))
            _c.close()
        except sqlite3.Error:
            pass
    time.sleep(ns.settle_observe_s)
    post_settle = None
    try:
        _c = ro_store(db)
        post_settle = (_c.execute(
            "SELECT MAX(seq) FROM state_events WHERE job_id=?",
            (job_id,)).fetchone()[0], len(_c.execute(
            "SELECT 1 FROM jobs").fetchall()))
        _c.close()
    except sqlite3.Error:
        pass
    no_activity_after_terminal = (terminal_seq is not None
                                  and post_settle == terminal_seq)

    # ---- C: verification (all read-only) ----
    v: dict = {"ts": now(), "job_id": job_id, "final_state": job_state,
               "state_timeline": seen_states}
    c = ro_store(db)
    try:
        v["baseline_jobs"] = sorted(baseline_jobs)
        v["jobs_all"] = rows(c, "SELECT job_id,state,requested_model,"
                                "result_revision,active_attempt_id,"
                                "created_at,updated_at FROM jobs")
        # requests' lifecycle column is `phase` (schema.sql) — the earlier
        # `state` selector was the watcher bug the user's round trip hit
        v["requests_all"] = rows(
            c, "SELECT request_id,tool,phase,job_id,created_at FROM requests")
        v["requests_for_job"] = [r for r in v["requests_all"]
                                 if r["job_id"] == job_id]
        att = rows(c, "SELECT * FROM attempts WHERE job_id=?", (job_id,))
        v["attempt"] = att
        attempt_id = att[0]["attempt_id"] if att else None
        v["state_events"] = rows(
            c, "SELECT seq,job_id,attempt_id,from_state,to_state,"
               "reason_code,observed_at FROM state_events WHERE job_id=?"
               " ORDER BY seq", (job_id,))
        bev = rows(c, "SELECT seq,attempt_seq,event_kind,session_id,"
                      "turn_id,source_event_id,observed_at FROM "
                      "backend_events WHERE attempt_id=? ORDER BY seq",
                   (attempt_id,))
        v["backend_events"] = bev
        v["artifacts"] = rows(
            c, "SELECT artifact_id,attempt_id,section,visibility,"
               "relative_path,media_type,byte_length,sha256,redacted,"
               "complete,result_revision FROM artifacts WHERE job_id=?",
            (job_id,))
        # raw session/turn extraction from the raw stdout artifact
        raw_sessions, raw_turns = [], []
        for a in v["artifacts"]:
            if a["section"] != "raw":
                continue
            # ArtifactStore layout: private/<attempt_id>/<relative_path>
            # (legacy fallback: artifacts/<relative_path>)
            candidates = [
                Path(paths["root"]) / "artifacts" / "private"
                / a["attempt_id"] / a["relative_path"],
                Path(paths["root"]) / "artifacts" / a["relative_path"],
            ]
            data = None
            for p in candidates:
                try:
                    data = p.read_bytes()
                    break
                except OSError:
                    continue
            if data is None:
                continue
            txt = data.decode("utf-8", "replace")
            raw_sessions += re.findall(r'"sessionID":"(ses_[^"]+)"', txt)
            raw_turns += re.findall(r'"messageID":"(msg_[^"]+)"', txt)
        v["raw_artifact_session_ids"] = sorted(set(raw_sessions))
        v["raw_artifact_turn_ids"] = sorted(set(raw_turns))
    finally:
        c.close()

    att0 = v["attempt"][0] if v["attempt"] else {}
    be_sessions = sorted({r["session_id"] for r in v["backend_events"]
                          if r["session_id"]})
    v["identity_check"] = {
        "attempt_session_id": att0.get("session_id"),
        "attempt_turn_id": att0.get("turn_id"),
        "backend_event_sessions": be_sessions,
        "raw_artifact_sessions": v["raw_artifact_session_ids"],
        "raw_artifact_turn_ids": v["raw_artifact_turn_ids"],
        "attempt_eq_backend": (att0.get("session_id") in be_sessions
                               if be_sessions else None),
        "backend_eq_raw": bool(be_sessions) and set(be_sessions) == set(
            v["raw_artifact_session_ids"]),
        # the session that ACTUALLY ran the prompt (raw CLI events) vs the
        # session LiveBackend.start created via the API and recorded on
        # the attempt — a mismatch is the documented P4 wiring finding
        # (OPEN_LIVE SESSION-ID): cancel/reconcile would target the
        # API session while the prompt ran in the raw session
        "attempt_eq_raw_run_session": (
            att0.get("session_id") in v["raw_artifact_session_ids"]
            if v["raw_artifact_session_ids"] else None),
        "findings": [],
    }
    if v["identity_check"]["attempt_eq_raw_run_session"] is False:
        v["identity_check"]["findings"].append(
            "attempts.session_id is the API-created session, NOT the "
            "session the CLI ran in (raw events); see HANDOFF-A3 "
            "SESSION-ID finding")
    if not v["backend_events"]:
        v["identity_check"]["findings"].append(
            "backend_events table empty for the live attempt; turn IDs "
            "are observable only in the raw artifact (messageID)")

    # server-side existence of the session(s) — read-only GET, never a
    # mutation and never other sessions
    sess_info = {}
    for sid in set([att0.get("session_id") or ""] + be_sessions
                   + v["raw_artifact_session_ids"]):
        if not sid:
            continue
        url = _server_url()
        try:
            p = subprocess.run(
                [os.path.expanduser("~/.opencode/bin/opencode"), "api",
                 "--server", url, "GET", "/api/session/%s" % sid],
                capture_output=True, timeout=20, env=os.environ.copy())
            body = json.loads(p.stdout.decode() or "{}")
            d = body.get("data") or {}
            sess_info[sid] = {"exit": p.returncode, "found": bool(d),
                              "status": (d.get("status")
                                         or (d.get("session") or {}).get(
                                             "status")),
                              "title": (d.get("title")
                                        or (d.get("session") or {}).get(
                                            "title"))}
        except Exception as e:
            sess_info[sid] = {"exit": None, "error": str(e)[:200]}
    v["server_sessions"] = sess_info

    # contract reads through the FIXED endpoint (post-completion re-query)
    st_env = endpoint_call(a3_stack.FIXED_HOST, a3_stack.FIXED_PORT,
                           "worker_status", {"job_id": job_id})
    res_env = endpoint_call(a3_stack.FIXED_HOST, a3_stack.FIXED_PORT,
                            "worker_result", {"job_id": job_id})
    res_env2 = endpoint_call(a3_stack.FIXED_HOST, a3_stack.FIXED_PORT,
                             "worker_result", {"job_id": job_id})
    v["status_envelope"] = st_env
    v["result_envelope"] = res_env
    v["result_requery_identical"] = (
        json.dumps(res_env, sort_keys=True)
        == json.dumps(res_env2, sort_keys=True))

    # endpoint hop log (connector-side connections)
    hop = Path(paths["logs"]) / "endpoint.err.log"
    v["endpoint_hop_log_tail"] = (hop.read_text(errors="replace")[-1200:]
                                  if hop.exists() else "")

    # ---- assertions (scoped to THIS round trip's job) ----
    checks = {}
    checks["completed"] = job_state == "completed"
    checks["one_new_job"] = len(v["jobs_all"]) == len(baseline_jobs) + 1
    checks["single_request_for_job"] = len(v["requests_for_job"]) == 1
    checks["request_is_worker_start"] = bool(v["requests_for_job"]) and \
        v["requests_for_job"][0]["tool"] == "worker_start"
    checks["no_other_mutation_tools"] = not any(
        r["tool"] != "worker_start" for r in v["requests_for_job"])
    checks["single_attempt"] = len(v["attempt"]) == 1
    checks["result_preserved"] = (
        isinstance(res_env, dict) and "error" not in res_env
        and res_env.get("tool") == "worker_result"
        and (res_env.get("data") or {}).get("items") is not None)
    checks["result_requery_identical"] = v["result_requery_identical"]

    # ---- delivery contract (M4-A3 round-1 blockage, both read-only) ----
    # defect 1: ids emitted art-<hex> were REFUSED by the connector —
    #           contract is art_<UUIDv4> (MCP.md §2, mcp-tools.json).
    # defect 2: the answer text never appeared in the summary the
    #           connector reads, so ChatGPT had nothing to deliver.
    _UUID4_AID = re.compile(
        r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
        r"-[0-9a-f]{12}$")
    summary_text = ""
    try:
        items = res_env.get("data", {}).get("items") or []
        if items and items[0].get("kind") == "summary":
            summary_text = items[0].get("text") or ""
    except AttributeError:
        pass
    v["delivery"] = {
        "summary_len": len(summary_text),
        "summary_head": summary_text[:500],
    }
    has_block = "answer:" in summary_text
    token_ok = (ns.expect_answer in summary_text) if ns.expect_answer \
        else has_block
    checks["summary_contains_answer"] = bool(has_block and token_ok)
    checks["artifact_ids_uuidv4"] = bool(v["artifacts"]) and all(
        _UUID4_AID.match(a["artifact_id"]) for a in v["artifacts"])
    # the connector's own view: worker_result section=artifacts over the
    # fixed endpoint (read-only contract read, same call ChatGPT makes)
    lst_env = endpoint_call(a3_stack.FIXED_HOST, a3_stack.FIXED_PORT,
                            "worker_result",
                            {"job_id": job_id, "section": "artifacts",
                             "limit": 50})
    lst_items = ((lst_env or {}).get("data") or {}).get("items") or []
    v["delivery"]["connector_artifact_ids"] = [
        i.get("artifact_id") for i in lst_items
        if i.get("kind") == "artifact"]
    checks["connector_list_ids_uuidv4"] = bool(lst_items) and all(
        _UUID4_AID.match(i.get("artifact_id") or "")
        for i in lst_items if i.get("kind") == "artifact")
    checks["session_identity_documented"] = bool(
        v["identity_check"]["backend_eq_raw"] is not None)
    v["settle"] = {"at_terminal": terminal_seq, "after_settle": post_settle,
                   "no_activity_after_terminal": no_activity_after_terminal}
    checks["no_auto_next_job"] = (
        checks["one_new_job"] and checks["single_request_for_job"]
        and checks["no_other_mutation_tools"] and no_activity_after_terminal)
    v["checks"] = checks
    hard = [k for k in ("completed", "one_new_job", "single_request_for_job",
                        "single_attempt", "result_preserved",
                        "result_requery_identical", "no_auto_next_job",
                        "summary_contains_answer", "artifact_ids_uuidv4",
                        "connector_list_ids_uuidv4")
            if not checks[k]]
    v["hard_failures"] = hard
    _dump(out, "roundtrip-verification.json", v)
    _dump(out, "roundtrip-summary.json",
          {"ts": now(), "final_state": job_state, "checks": checks,
           "identity": v["identity_check"],
           "hard_failures": hard})
    log("verification written; hard_failures=%s" % hard)
    if hard:
        return 1
    if not (v["identity_check"]["attempt_eq_backend"]
            and v["identity_check"]["backend_eq_raw"]):
        log("NOTE: session identity mismatch documented (finding, exit 2)")
        return 2
    return 0


_SERVER_URL_CACHE = None


def _server_url() -> str:
    global _SERVER_URL_CACHE
    if _SERVER_URL_CACHE is None:
        _SERVER_URL_CACHE = a3_stack.discover_server_url()
    return _SERVER_URL_CACHE


def _dump(out: Path, name: str, obj) -> None:
    (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1,
                                       default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
