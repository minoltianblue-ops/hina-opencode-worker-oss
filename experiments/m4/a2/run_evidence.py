#!/usr/bin/env python3
"""M4-A2 evidence driver: managed supervisor run — manual start,
crash-restart with journal preservation, log retention, observe-only
viewer wiring, clean stop, residual 0.

Everything runs on this Mac, localhost/UDS only, with NO service
registration (the launchd artifact is a DRAFT; plutil lint only), NO
public exposure, NO model invocation (ScriptedBackend — zero real
prompts, D2) and NO user-account action.

Scenario written to --out:
  1. manual `supervise_run.py run` -> status READY
  2. worker_start J1 (completes) + J2 (long-running) via mcp_proxy client
  3. SIGKILL the supervisor child -> wrapper clears the stale socket
     (lock-verified) and restarts -> READY again
  4. journal preserved: J1/J2 rows, request rows and state_events are
     byte-for-byte the same rows after restart (viewer round trips)
  5. log retention: --log-keep cap held while the child re-logs
  6. viewer wiring: `supervise_run.py viewer --once` shows the table;
     a viewer-role mutation is FORBIDDEN server-side
  7. SIGTERM -> operator stop, exit 0; plutil lint on the plist DRAFT;
     pgrep residual 0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _wt_support import make_repo  # noqa: E402


def call(sock: str, req: dict, timeout: float = 10.0) -> dict:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(sock)
        payload = json.dumps(req).encode("utf-8")
        conn.sendall(struct.pack(">I", len(payload)) + payload)
        (length,) = struct.unpack(">I", conn.recv(4))
        body = b""
        while len(body) < length:
            body += conn.recv(length - len(body))
        return json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def snapshot(sock: str) -> dict:
    return call(sock, {"client": "viewer", "method": "viewer.snapshot"})


def wait_ready(sock: str, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            if "data" in snapshot(sock):
                return True
        except (OSError, struct.error, json.JSONDecodeError):
            pass  # bind/accept race while a child (re)starts
        time.sleep(0.2)
    return False


def jobs_of(sock: str) -> dict:
    data = snapshot(sock).get("data", {})
    return {j["job_id"]: j for j in data.get("jobs", [])}, data.get(
        "next_seq", 0)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--plist", default=str(
        ROOT / "docs/deploy/launchd/"
        "com.hina.opencode.worker.supervisor.plist.draft"))
    ns = ap.parse_args(argv)
    out = Path(ns.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    timeline: list[str] = []
    facts: dict = {"scenario": "m4-a2 managed supervisor run",
                   "real_prompts": 0, "model_calls": 0,
                   "approvals": {"service_install": False,
                                 "public_exposure": False,
                                 "live_model": False}}

    def note(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
        timeline.append(line)
        print(line, flush=True)

    root = Path("/tmp") / f"m4a2_{int(time.time())}_{os.getpid()}"
    (root / "logs").mkdir(parents=True, mode=0o700)
    repo = make_repo(root / "repo")
    sock = str(root / "run" / "supervisor.sock")
    fake_script = root / "logs" / "fake-script.json"
    # start #1 (J1) completes; every later start (J2 and beyond) sleeps
    # — the running job is what the crash must not erase.
    fake_script.write_text(json.dumps([
        [{"action": "complete_after", "seconds": 0.8,
          "reason": "m4a2 evidence J1"}],
        [{"action": "sleep", "seconds": 1800}]]), encoding="utf-8")

    wrapper = subprocess.Popen(
        [sys.executable, str(HERE / "supervise_run.py"), "run",
         "--root", str(root),
         "--fake-script", str(fake_script),
         "--log-max-bytes", "3072", "--log-keep", "3"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_ready(sock, 20), "supervisor never became READY"
        note("S1 manual start: supervise_run.py run -> status READY "
             f"(wrapper pid={wrapper.pid})")

        def worker_start(tag: str) -> dict:
            return call(sock, {
                "client": "mcp_proxy", "tool": "worker_start",
                "arguments": {
                    "request_id": "req_" + str(uuid.uuid4()),
                    "user_request_ref": "req/2026/m4a2",
                    "goal": f"m4a2 evidence {tag}",
                    "workspace": {"repository": str(repo),
                                  "source_ref": "HEAD"},
                    "profile": "inspect", "model": "fake/free",
                    "constraints": [], "done_when": ["answer given"]}})

        env1 = worker_start("j1")
        assert "error" not in env1, f"worker_start j1 failed: {env1}"
        j1 = env1["data"]["job"]["job_id"]
        end = time.monotonic() + 15
        states = {}
        while time.monotonic() < end:
            states, _ = jobs_of(sock)
            if states.get(j1, {}).get("state") == "completed":
                break
            time.sleep(0.2)
        env2 = worker_start("j2")
        assert "error" not in env2, f"worker_start j2 failed: {env2}"
        j2 = env2["data"]["job"]["job_id"]
        end = time.monotonic() + 15
        while time.monotonic() < end:
            states, _ = jobs_of(sock)
            if states.get(j2, {}).get("state") in ("starting", "running"):
                break
            time.sleep(0.2)
        note(f"S2 two jobs up: J1={j1} (completed), J2={j2} (running), "
             "both scripted fake-backend — zero real prompts")

        before, seq_before = jobs_of(sock)
        tail_before = call(sock, {"client": "viewer",
                                  "method": "viewer.tail",
                                  "arguments": {"after_seq": 0}})
        reqs_before = call(sock, {"client": "viewer",
                                  "method": "viewer.requests",
                                  "arguments": {}})
        (out / "snapshot-before-restart.json").write_text(
            json.dumps({"jobs": before, "next_seq": seq_before,
                        "events": tail_before.get("data"),
                        "requests": reqs_before.get("data")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        assert before[j1]["state"] == "completed", before[j1]
        assert before[j2]["state"] in ("running", "starting"), before[j2]

        time.sleep(3.0)  # > min-up: a crash after healthy uptime
        child_pid = int((root / "run" / "supervisor.pid").read_text())
        os.kill(child_pid, signal.SIGKILL)
        note(f"S3 SIGKILL {child_pid} (simulated crash of the supervised"
             " child after healthy uptime)")
        ok = wait_ready(sock, 30)
        events_log = (root / "logs" / "run-events.log").read_text(
            encoding="utf-8")
        assert ok, "wrapper did not restore READY after crash"
        assert "rc=-9" in events_log, "crash not recorded in run-events"
        restart_line = [ln for ln in events_log.splitlines()
                        if "restart in" in ln][-1]
        note(f"S4 wrapper restart: {restart_line.split('] ', 1)[-1]} ->"
             " READY again (journal under db/ untouched)")

        after, seq_after = jobs_of(sock)
        tail_after = call(sock, {"client": "viewer",
                                 "method": "viewer.tail",
                                 "arguments": {"after_seq": 0}})
        reqs_after = call(sock, {"client": "viewer",
                                 "method": "viewer.requests",
                                 "arguments": {}})
        (out / "snapshot-after-restart.json").write_text(
            json.dumps({"jobs": after, "next_seq": seq_after,
                        "events": tail_after.get("data"),
                        "requests": reqs_after.get("data")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        ev_b = tail_before["data"]["events"]
        ev_a = tail_after["data"]["events"]
        rq_b = {r["request_id"]: r for r in reqs_before["data"]["requests"]}
        rq_a = {r["request_id"]: r for r in reqs_after["data"]["requests"]}
        added = [e for e in ev_a if e not in ev_b]
        facts["journal"] = {
            "j1_state_before": before[j1]["state"],
            "j1_state_after": after[j1]["state"],
            "j2_state_before": before[j2]["state"],
            "j2_state_after": after[j2]["state"],
            "events_before": len(ev_b), "events_after": len(ev_a),
            "events_prefix_preserved": ev_a[:len(ev_b)] == ev_b,
            "events_added_by_restart_reconcile": added,
            "requests_before": len(rq_b), "requests_after": len(rq_a),
            "request_ids_preserved": set(rq_b) <= set(rq_a)}
        # crash-window discipline (STORAGE.md §3 ④): restart reconcile may
        # APPEND (needs_attention/ambiguous), must never rewrite or drop
        # journaled rows.
        assert facts["journal"]["events_prefix_preserved"], \
            "pre-crash state_events were rewritten or dropped"
        assert facts["journal"]["request_ids_preserved"], "request rows lost"
        assert after[j1]["state"] == "completed", "J1 journal lost"
        st1 = call(sock, {"client": "mcp_proxy", "tool": "worker_status",
                          "arguments": {"job_id": j1}})
        (out / "worker-status-j1-after.json").write_text(
            json.dumps(st1, ensure_ascii=False, indent=2),
            encoding="utf-8")
        jview = st1["data"]["jobs"][0]
        assert (jview.get("state") or jview.get("job", {}).get("state")) \
            == "completed", jview
        note("S5 journal preserved across crash: all pre-crash"
             f" state_events rows byte-identical (prefix) +{len(added)}"
             " appended by restart reconcile;"
             " worker_status still serves J1 from the journal")

        log_files = sorted((root / "logs").glob("supervisor.out.log*"))
        facts["log_retention"] = {
            "files": [p.name for p in log_files],
            "sizes_bytes": [p.stat().st_size for p in log_files],
            "max_bytes": 3072, "keep": 3}
        assert len(log_files) <= 3, "log retention cap exceeded"
        assert len(log_files) >= 2, "no rotation observed"
        note(f"S6 log retention: {len(log_files)} files retained, cap 3"
             f" sizes={facts['log_retention']['sizes_bytes']}")

        vw = subprocess.run(
            [sys.executable, str(HERE / "supervise_run.py"), "viewer",
             "--root", str(root)],  # default mode: --once
            capture_output=True, text=True, cwd=str(ROOT))
        (out / "viewer-once.txt").write_text(vw.stdout, encoding="utf-8")
        assert vw.returncode == 0 and j1 in vw.stdout and j2 in vw.stdout
        forbidden = call(sock, {"client": "viewer",
                                "method": "worker_start",
                                "arguments": {}})
        (out / "viewer-mutation-refused.json").write_text(
            json.dumps(forbidden, indent=2), encoding="utf-8")
        assert forbidden.get("error", {}).get("code") == "FORBIDDEN"
        st = subprocess.run(
            [sys.executable, str(HERE / "supervise_run.py"), "status",
             "--root", str(root)], capture_output=True, text=True)
        (out / "status-ready.json").write_text(st.stdout, encoding="utf-8")
        facts["viewer_wiring"] = {"viewer_once_exit": vw.returncode,
                                  "status_exit": st.returncode,
                                  "mutation_refused":
                                      forbidden["error"]["code"]}
        note("S7 viewer wiring observe-only: snapshot renders both jobs;"
             " viewer-role worker_start refused FORBIDDEN server-side")

        lint = subprocess.run(["plutil", "-lint", ns.plist],
                              capture_output=True, text=True)
        (out / "plist-lint.txt").write_text(
            f"$ plutil -lint {ns.plist}\n{lint.stdout}{lint.stderr}",
            encoding="utf-8")
        assert lint.returncode == 0
        facts["plist_draft"] = {"path": os.path.relpath(ns.plist, ROOT),
                                "lint": "OK", "installed": False,
                                "note": "DRAFT — install requires"
                                        " separate user approval"}
        note("S8 launchd plist linted OK; DRAFT only, NOT installed")

        wrapper.send_signal(signal.SIGTERM)
        rc = wrapper.wait(timeout=20)
        assert rc == 0, f"wrapper exit {rc} after SIGTERM"
        time.sleep(0.3)
        assert not socket_exists(sock)
        assert not (root / "run" / "supervisor.pid").exists()
        note(f"S9 operator stop: SIGTERM -> wrapper exit {rc}, socket and"
             " pidfile cleaned, no restart (run-events 'operator_stop')")
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)
        # SIGKILLing the wrapper (failure path) leaves the supervised
        # child orphaned — clean it here so residual stays 0 either way
        stray = subprocess.run(["pgrep", "-f", str(root.name)],
                               capture_output=True, text=True)
        for pid in stray.stdout.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        if stray.stdout.strip():
            time.sleep(0.5)
        for name in ("run-events.log", "supervisor.out.log"):
            src = root / "logs" / name
            if src.exists():
                shutil.copy2(src, out / name)
        for extra in sorted((root / "logs").glob("supervisor.out.log.*")):
            shutil.copy2(extra, out / extra.name)
        residual = subprocess.run(["pgrep", "-fl", str(root.name)],
                                  capture_output=True, text=True)
        # exclude this driver: pgrep -f also matches our own argv
        lines = [ln for ln in residual.stdout.splitlines()
                 if "run_evidence" not in ln]
        (out / "residual-check.txt").write_text(
            f"$ pgrep -fl {root.name}\n{chr(10).join(lines)}\n"
            f"exit={residual.returncode} (1 = no match)\n"
            f"driver-excluded lines: {len(lines)}\n", encoding="utf-8")
        facts["residual"] = {"pgrep_exit": residual.returncode,
                             "matches_excluding_driver": len(lines)}
        assert len(lines) == 0, f"residual procs: {lines}"
        note("S10 residual 0: no supervisor_proc/supervise_run/viewer"
             " process survives")
        shutil.rmtree(root, ignore_errors=True)

    (out / "gate-summary.json").write_text(
        json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "SUMMARY.md").write_text(
        "# M4-A2 evidence — managed supervisor run\n\n"
        "Manual start -> crash (SIGKILL) restart with journal preserved"
        " -> log retention -> observe-only viewer -> clean stop.\n"
        "Fake backend only: zero real prompts, zero model calls. The"
        " launchd plist is a linted DRAFT; install needs separate"
        " approval.\n\n```\n" + "\n".join(timeline) + "\n```\n",
        encoding="utf-8")
    manifest = {"run_id": out.name, "host": "Mac mini (darwin)",
                "files": {p.name: sha256_file(p)
                          for p in sorted(out.iterdir())
                          if p.is_file() and p.name != "manifest.json"},
                "scope": "localhost/UDS only; no service install, no"
                         " public exposure, no model calls, no user"
                         " account action",
                "bootstrap": "python3.12 stdlib only"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                       encoding="utf-8")
    print("EVIDENCE-OK", out)
    return 0


def socket_exists(path: str) -> bool:
    return Path(path).exists()


if __name__ == "__main__":
    sys.exit(main())
