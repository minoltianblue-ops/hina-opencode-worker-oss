#!/usr/bin/env python3
"""M4-A1 evidence driver: localhost tunnel up, health != readiness,
blocked-until-ready proof.

Runs entirely on this Mac, localhost only, with NO public exposure, NO
service registration, NO model invocation and NO user-account action
(M4-A1 scope). The supervisor runs in-process with the scripted
FakeBackend — deliberately, so the evidence contains zero real prompts.
The OpenCode *binary pin* tier reads the real, M0-pinned binary
read-only (`--version` + sha256).

Scenario written to --out:
  1. endpoint (localhost-only) + proxy up, supervisor DOWN
       -> health PASS, readiness FAIL (supervisor tier)
  2. gate probe starts blocking; supervisor starts mid-gate
       -> gate log shows NOT_READY...READY transition (blocked-until-ready)
  3. --bind 0.0.0.0 is REFUSED (exit 2, stderr) — no listener ever bound
  4. transport disconnect kills only the short-lived proxy child;
     supervisor untouched; readiness stays READY
  5. SIGTERM => clean shutdown, info file removed, zero residual procs
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from hina_opencode_worker.supervisor import (  # noqa: E402
    ModelCatalog, Supervisor, SupervisorConfig)
from _p4_support import Clock, FakeBackend  # noqa: E402

TOOLS = ROOT / "docs" / "contracts" / "mcp-tools.json"
ENDPOINT = ROOT / "experiments" / "m4" / "a1" / "tunnel_endpoint.py"
PROBE = ROOT / "experiments" / "m4" / "a1" / "readiness_probe.py"
OPENCODE_BIN = os.environ.get("OPENCODE_BIN",
                              os.path.expanduser("~/.opencode/bin/opencode"))


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + \
        ".%03dZ" % (int(time.time() * 1000) % 1000)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def run_probe(out: Path, args: list[str], name: str) -> dict:
    cmd = [sys.executable, str(PROBE)] + args + ["--log-file",
                                                 str(out / "probe-log.jsonl")]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    (out / name).write_text(p.stdout, encoding="utf-8")
    if p.stderr:
        (out / (name + ".stderr")).write_text(p.stderr, encoding="utf-8")
    try:
        doc = json.loads(p.stdout)
    except json.JSONDecodeError:
        doc = {"parse_error": True, "raw": p.stdout[:500]}
    return {"exit": p.returncode, "doc": doc}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ns = ap.parse_args(argv)
    out = Path(ns.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    timeline: list[tuple[str, str]] = []

    def mark(msg: str) -> None:
        timeline.append((ts(), msg))
        print("%s %s" % (timeline[-1][0], msg), flush=True)

    # macOS AF_UNIX paths must stay under ~104 chars, so the supervisor
    # root lives in a short /tmp dir (removed in finally; it is not
    # evidence) while the evidence artifacts live under --out.
    tmp = Path(tempfile.mkdtemp(prefix="m4a1_", dir="/tmp"))
    sock_path = tmp / "run" / "supervisor.sock"
    sup = None
    ep = None
    ep_log = None
    fail: list[str] = []
    try:
        # ---- 1. endpoint + proxy up, supervisor DOWN -----------------
        info_file = out / "endpoint-info.json"
        ep_log = open(out / "endpoint.stderr.log", "w", encoding="utf-8")
        ep = subprocess.Popen(
            [sys.executable, str(ENDPOINT), "--socket", str(sock_path),
             "--tools", str(TOOLS), "--port", "0",
             "--info-file", str(info_file)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=ep_log,
            env={**os.environ, "PYTHONPATH":
                 str(ROOT / "src") + ":" + str(ROOT / "tests")})
        deadline = time.time() + 10
        while not info_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert info_file.exists(), "endpoint info file never appeared"
        info = json.loads(info_file.read_text())
        (out / "endpoint-info.captured.json").write_text(
            json.dumps(info, indent=2), encoding="utf-8")
        assert info["bound_host"] == "127.0.0.1" and info["loopback_only"]
        mark("endpoint up: 127.0.0.1:%d pid=%d (supervisor NOT started)"
             % (info["port"], info["pid"]))

        h = run_probe(out, ["health", "--info-file", str(info_file)],
                      "probe-health.json")
        assert h["exit"] == 0 and h["doc"]["verdict"] == "HEALTHY", h
        mark("health PASS with backend down (endpoint+proxy ping) — "
             "health does NOT depend on the backend")

        r = run_probe(out, ["readiness", "--info-file", str(info_file),
                            "--binary", OPENCODE_BIN],
                      "probe-readiness-backend-down.json")
        sup_check = r["doc"]["checks"]["supervisor"]
        assert r["exit"] == 1 and sup_check["ok"] is False, r
        mark("readiness FAIL with backend down (supervisor tier: %s) — "
             "health PASS + readiness FAIL = split demonstrated"
             % sup_check["detail"])

        # ---- 2. blocked-until-ready ------------------------------------
        gate_proc = subprocess.Popen(
            [sys.executable, str(PROBE), "gate",
             "--info-file", str(info_file), "--binary", OPENCODE_BIN,
             "--timeout", "25", "--interval", "0.25",
             "--log-file", str(out / "gate-log.jsonl")],
            cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
        time.sleep(1.5)
        # NOTE: do NOT pre-create tmp/run — Supervisor creates it 0700 and
        # _audit_transport refuses a wider mode.
        catalog = ModelCatalog(profiles=("inspect", "develop"), gate="PASS",
                               capabilities={"develop": True})
        # Catalog policy mirrors the M0-signed state; probe_status is our
        # ledger, NOT a live claim. No model is invoked by this run.
        catalog.add("opencode/glm-5.3-flash-free", "free",
                    probe_status="live_verified")
        catalog.add("opencode/synthetic-off", "free",
                    probe_status="unavailable")
        sup = Supervisor(SupervisorConfig(root=str(tmp)),
                         backend=FakeBackend(), catalog=catalog,
                         clock=Clock().monotonic, time_fn=Clock().time)
        sup.start_uds()
        mark("supervisor UDS started mid-gate at %s" % sock_path)
        gate_out = gate_proc.communicate(timeout=40)[0].strip()
        (out / "gate-summary.json").write_text(gate_out, encoding="utf-8")
        assert gate_proc.returncode == 0, gate_out
        gate_doc = json.loads(gate_out)
        log_lines = [json.loads(l) for l in
                     (out / "gate-log.jsonl").read_text().splitlines()]
        gate_attempts = [l for l in log_lines if l["probe"] == "readiness"]
        first = gate_attempts[0]
        last = gate_attempts[-1]
        assert first["verdict"] == "NOT_READY" \
            and first["checks"]["supervisor"]["ok"] is False
        assert last["verdict"] == "READY", last
        assert gate_doc["attempts"] == len(gate_attempts) >= 2
        mark("gate blocked %ss over %d attempts then READY — "
             "blocked-until-ready proof in probe-log.jsonl"
             % (gate_doc["blocked_s"], gate_doc["attempts"]))

        rr = run_probe(out, ["readiness", "--info-file", str(info_file),
                             "--binary", OPENCODE_BIN],
                       "probe-readiness-ready.json")
        assert rr["exit"] == 0 and rr["doc"]["verdict"] == "READY", rr
        lm = rr["doc"]["checks"]["live_model"]
        assert lm["status"] == "BLOCKED_NO_APPROVAL", lm
        mark("readiness READY; live_model tier %s (no model call, "
             "no account action)" % lm["status"])

        # ---- 3. non-localhost bind refused ------------------------------
        refused = subprocess.run(
            [sys.executable, str(ENDPOINT), "--socket", str(sock_path),
             "--tools", str(TOOLS), "--bind", "0.0.0.0", "--port", "0"],
            capture_output=True, text=True, timeout=15)
        (out / "bind-refused.stderr.log").write_text(refused.stderr,
                                                     encoding="utf-8")
        assert refused.returncode == 2 and "REFUSED" in refused.stderr, \
            refused.stderr
        mark("0.0.0.0 bind REFUSED exit=2 before bind() — no public "
             "listener can exist in this scope")

        # ---- 4. disconnect kills only the proxy child; supervisor OK ----
        conn = socket.create_connection(("127.0.0.1", info["port"]),
                                        timeout=5)
        conn.sendall((json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "m4a1-disconnect-test",
                                       "version": "0"}}}) + "\n").encode())
        conn.recv(65536)
        conn.close()
        deadline = time.time() + 10
        child_pids = []
        while time.time() < deadline:
            ps = subprocess.run(["ps", "-o", "pid=", "--ppid",
                                 str(info["pid"])],
                                capture_output=True, text=True)
            child_pids = [p for p in ps.stdout.split() if p.strip()]
            if not child_pids:
                break
            time.sleep(0.1)
        orphan = run_probe(out, ["readiness", "--info-file", str(info_file),
                                 "--binary", OPENCODE_BIN],
                           "probe-readiness-after-disconnect.json")
        assert orphan["exit"] == 0, orphan
        with open(out / "orphan-and-survival.txt", "w",
                  encoding="utf-8") as fh:
            fh.write("endpoint pid: %d\n" % info["pid"])
            fh.write("endpoint children after client disconnect: %s\n"
                     % (child_pids or "none — short-lived proxy reaped"))
            fh.write("supervisor readiness after transport death: %s\n"
                     % orphan["doc"]["verdict"])
        assert not child_pids, child_pids
        mark("client disconnect: proxy child reaped, supervisor alive "
             "(T15 transport death != job cancel)")

        # ---- 5. clean shutdown ------------------------------------------
        ep.terminate()
        rc = ep.wait(timeout=10)
        assert rc == 0, rc
        assert not info_file.exists(), "info file not cleaned"
        sup.close()
        sup = None
        pg = subprocess.run(["pgrep", "-f",
                             "hina_opencode_worker.mcp_proxy|"
                             "tunnel_endpoint.py"], capture_output=True,
                            text=True)
        residual = pg.stdout.split()
        with open(out / "residual-check.txt", "w", encoding="utf-8") as fh:
            fh.write("endpoint exit: 0 (SIGTERM clean)\n")
            fh.write("info file removed: %s\n" % (not info_file.exists()))
            fh.write("pgrep residual proxy/endpoint pids: %s\n"
                     % (residual or "none"))
        assert not residual, residual
        mark("clean shutdown, zero residual procs/files outside evidence")
    except AssertionError as e:
        fail.append("ASSERT FAILED: %s" % e)
        mark("SCENARIO FAILED: %s (partial evidence kept)" % e)
    finally:
        if sup is not None:
            try:
                sup.close()
            except Exception:
                pass
        if ep is not None and ep.poll() is None:
            ep.kill()
            ep.wait(timeout=5)
        try:
            ep_log.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- manifest + summary --------------------------------------------
    files = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and "m4a1root_" not in p.parts:
            files[str(p.relative_to(out))] = sha256(p)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    manifest = {
        "kind": "m4a1-evidence-manifest",
        "run_at": timeline[0][0], "run_finished": ts(),
        "source_sha": head,
        "host": os.uname().nodename, "platform": sys.platform,
        "opencode_binary": OPENCODE_BIN,
        "scope": "localhost-only; no public exposure; no service "
                 "registration; no model invocation; no user-account "
                 "action; no push",
        "approvals": {
            "public_exposure": "NOT DONE (separate approval required)",
            "permanent_service": "NOT DONE (separate approval required)",
            "live_model_probe": "NOT RUN (BLOCKED_NO_APPROVAL; dormant "
                                "tier, see readiness_probe.py)"},
        "files": files,
        "errors": fail,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                       encoding="utf-8")
    with open(out / "SUMMARY.md", "w", encoding="utf-8") as fh:
        fh.write("# M4-A1 evidence summary\n\n")
        for when, msg in timeline:
            fh.write("- `%s` %s\n" % (when, msg))
        fh.write("\nAcceptance mapping:\n"
                 "- localhost tunnel up: endpoint-info.captured.json "
                 "(127.0.0.1 only), bind-refused.stderr.log\n"
                 "- health != readiness: probe-health.json (PASS while "
                 "backend down) vs probe-readiness-backend-down.json "
                 "(FAIL)\n"
                 "- blocked-until-ready: gate-log.jsonl (NOT_READY -> "
                 "READY under gate) + gate-summary.json\n"
                 "- probe audit trail (single probes): probe-log.jsonl\n"
                 "- residuals: residual-check.txt, "
                 "orphan-and-survival.txt\n")
    print("EVIDENCE OK -> %s" % out)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
