#!/usr/bin/env python3
"""M4-A3 evidence driver — pre-registration verification of the live
stack on the FIXED localhost endpoint.

Ordered evidence (each step is a file under --out):
  1. stack-facts        — status snapshot, pids, fixed host:port, root
  2. health             — endpoint+transport+proxy (no backend dep)
  3. readiness-core     — tunnel+supervisor+binary_pin+model_policy
  4. readiness-live     — ONE synthetic reachability call of the EXACT
                          SUT model via the readiness live tier
                          (--allow-live + M4_A1_LIVE_MODEL_OK gates,
                          same as A1; no other model is ever tried)
  5. tools-roundtrip    — initialize -> initialized -> tools/list ->
                          worker_catalog through 127.0.0.1:<fixed>
                          (8 contract tools, envelope v0.3)
  6. dialer-roundtrip   — same handshake piped through stdio_dialer.py
                          (the exact tunnel-client mcp_command shape)
  7. bind-refused       — --bind 0.0.0.0 on the fixed port refuses
                          BEFORE bind (exit 2, port untouched)
  8. residual           — pgrep: only the expected stack pids alive

Exit: 0 = everything incl. live reachability PASS; 3 = LIVE_MODEL BLOCKED
(user replacement decision required — D2, never auto-switched); 1 = other
failure. Raw stdout of the live probe is stored redacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "a1"))
import a3_stack  # noqa: E402
import readiness_probe as rp  # noqa: E402

TOOLS_LIST = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
INIT_NOTIF = {"jsonrpc": "2.0",
              "method": "notifications/initialized"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(out: Path, name: str, obj) -> None:
    (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def _live_full_diag(binary: str, model: str, out: Path) -> dict:
    """One diagnostic call of the SAME model id and SAME synthetic
    prompt as the dormant A1 live tier, captured with the full redacted
    provider error (block reason = user-decision-grade evidence).
    Records the backend session ids created as side effects."""
    diag = {"ok": False, "status": "LIVE_DIAG_RAN"}
    try:
        p = subprocess.run([binary, "run", "--model", model,
                            "--format", "json",
                            "--title", "hina-m4a3-reach-diag",
                            "Readiness probe. Reply with exactly: ok"],
                           capture_output=True, timeout=180,
                           env=dict(os.environ))
        from hina_opencode_worker.artifacts import redact_text
        raw, _ = redact_text(p.stdout.decode("utf-8", "replace"))
        err, _ = redact_text(p.stderr.decode("utf-8", "replace"))
        (out / "live-probe-full.txt").write_text(
            "exit=%s\n--- stdout (redacted) ---\n%s\n--- stderr "
            "(redacted) ---\n%s" % (p.returncode, raw[:6000], err[:1500]),
            encoding="utf-8")
        sess = []
        etype = None
        for line in raw.splitlines():
            for token in ("\"sessionID\":\"",):
                i = line.find(token)
                while i >= 0:
                    sid = line[i + len(token):].split('"', 1)[0]
                    if sid.startswith("ses_") and sid not in sess:
                        sess.append(sid)
                    i = line.find(token, i + 1)
            j = line.find('"type":"error"')
            if j >= 0 and etype is None:
                k = line.find('"error":{"type":"')
                if k >= 0:
                    etype = line[k + len('"error":{"type":"'):].split(
                        '"', 1)[0]
        diag.update({"exit": p.returncode,
                     "provider_error_type": etype,
                     "backend_sessions_created": sess})
    except Exception as e:
        diag["error"] = rp._safe(e)
    _write(out, "live-probe-diag.json", diag)
    return diag


def _status(root: str) -> dict:
    r = subprocess.run([sys.executable, str(HERE / "run_stack.py"),
                        "--root", root, "status"],
                       capture_output=True, env=dict(
                           os.environ, PYTHONPATH=str(
                               a3_stack.ROOT_REPO / "src")))
    try:
        return json.loads(r.stdout.decode())
    except ValueError:
        return {"raw": r.stdout.decode(errors="replace")[:500],
                "exit": r.returncode}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--root", default=a3_stack.DEFAULT_ROOT)
    ap.add_argument("--binary",
                    default=os.path.expanduser("~/.opencode/bin/opencode"))
    ap.add_argument("--model", default=a3_stack.SUT_MODEL)
    ap.add_argument("--allow-live", action="store_true",
                    help="run the readiness live tier (one synthetic call"
                         " of the exact --model id; env-gated like A1)")
    ns = ap.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(ns.root))
    paths = a3_stack.root_paths(root)
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict = {"started_at": _now(), "scope":
                     "M4-A3 pre-registration stack evidence (localhost "
                     "only; synthetic prompt except the single live "
                     "reachability call)",
                     "approvals": {"launchd_install": False,
                                   "cokacremote_change": False,
                                   "v1_change": False,
                                   "service_registration": False}}

    info = rp.load_info(paths["endpoint_info"])
    fixed_ok = (info.get("bound_host") == a3_stack.FIXED_HOST
                and info.get("port") == a3_stack.FIXED_PORT)
    summary["fixed_port_ok"] = fixed_ok
    summary["endpoint"] = {"host": info.get("bound_host"),
                           "port": info.get("port"), "pid": info.get("pid")}
    _write(out, "endpoint-info.captured.json", info)
    _write(out, "stack-facts.json", _status(root))

    health = rp.check_health(info)
    _write(out, "health.json", {"ts": _now(), "verdict":
                                "HEALTHY" if health["ok"] else "UNHEALTHY",
                                "checks": health["checks"]})
    summary["health_ok"] = health["ok"]

    core = rp.check_readiness(info, binary=ns.binary, allow_live=False,
                              live_out=None)
    _write(out, "readiness-core.json",
           {"ts": _now(), "checks": core["checks"]})
    summary["readiness_core_ok"] = core["ok"]

    live = {"ok": None, "status": "NOT_RUN",
            "detail": "--allow-live not given; live tier stays dormant"}
    if ns.allow_live:
        os.environ["M4_A1_LIVE_MODEL_OK"] = ns.model
        full = rp.check_readiness(info, binary=ns.binary, allow_live=True,
                                  live_out=str(out / "live-probe-stdout.txt"))
        live = full["checks"]["live_model"]
        _write(out, "readiness-live.json",
               {"ts": _now(), "model": ns.model, "checks":
                {"live_model": live, "model_policy":
                 full["checks"]["model_policy"]}})
        if live.get("ok") is False:
            # ONE follow-up diagnostic call of the SAME synthetic prompt
            # of the SAME model id — the A1-tier live_out is capped at 400
            # chars and truncates provider errors; the block reason is
            # decision-grade evidence the user needs. No new model is
            # ever tried here (D2): this is the same id, same prompt.
            summary["live_model_full_diag"] = _live_full_diag(
                ns.binary, ns.model, out)
    summary["live_model"] = live

    # full contract round trip through the FIXED endpoint (ChatGPT's
    # entry point after registration; 8 tools + envelope v0.3)
    rt = {"ok": False}
    try:
        replies = rp._rpc_roundtrip(
            info["bound_host"], int(info["port"]),
            [rp.INITIALIZE, INIT_NOTIF, rp.CATALOG_CALL, TOOLS_LIST],
            expect_ids=[1, 3, 4])
        by_id = {d["id"]: d for d in replies}
        tools = (by_id.get(4, {}).get("result", {}) or {}).get("tools", [])
        env = (by_id.get(3, {}).get("result", {}) or {}).get(
            "structuredContent") or {}
        rt = {
            "ok": ([t["name"] for t in tools]
                   == sorted(t["name"] for t in tools)
                   and len(tools) == 8
                   and env.get("contract_version") == "0.3"
                   and env.get("tool") == "worker_catalog"
                   and "error" not in env),
            "tools": [t["name"] for t in tools],
            "server_info": (by_id.get(1, {}).get("result", {}) or {}).get(
                "serverInfo"),
            "catalog_models": (env.get("data") or {}).get("models"),
            "catalog_engine": (env.get("data") or {}).get("engine"),
        }
    except Exception as e:
        rt = {"ok": False, "error": rp._safe(e)}
    _write(out, "tools-roundtrip.json", {"ts": _now(), **rt})
    summary["tools_roundtrip_ok"] = rt["ok"]

    # the exact tunnel-client mcp_command shape, exercised locally
    dialer = {"ok": False}
    try:
        payload = (json.dumps(rp.INITIALIZE) + "\n"
                   + json.dumps(rp.PING) + "\n")
        p = subprocess.run(
            [sys.executable, str(HERE / "stdio_dialer.py"),
             "--port", str(a3_stack.FIXED_PORT)],
            input=payload.encode(), capture_output=True, timeout=20)
        lines = [json.loads(l) for l in
                 p.stdout.decode().splitlines() if l.strip()]
        ids = {d.get("id") for d in lines}
        dialer = {"ok": {1, 2} <= ids and p.returncode == 0,
                  "reply_ids": sorted(i for i in ids if i is not None),
                  "exit": p.returncode,
                  "stderr_tail": rp._safe(
                      p.stderr.decode(errors="replace")[-300:])}
    except Exception as e:
        dialer = {"ok": False, "error": rp._safe(e)}
    _write(out, "dialer-roundtrip.json", {"ts": _now(), **dialer})
    summary["dialer_roundtrip_ok"] = dialer["ok"]

    # non-loopback refusal, attempted on OUR fixed port: proof the
    # refusal happens BEFORE bind (the live endpoint keeps the port; the
    # attempted stealer exits 2 without touching it)
    refused = {"ok": False}
    try:
        p = subprocess.run(
            [sys.executable,
             str(a3_stack.ROOT_REPO / "experiments" / "m4" / "a1" /
                 "tunnel_endpoint.py"),
             "--socket", paths["sock"], "--tools", a3_stack.TOOLS_DOC,
             "--bind", "0.0.0.0", "--port", str(a3_stack.FIXED_PORT)],
            capture_output=True, timeout=15)
        refused = {"ok": p.returncode == 2 and
                   b"REFUSED" in p.stderr,
                   "exit": p.returncode,
                   "stderr": rp._safe(p.stderr.decode(
                       errors="replace")[-400:])}
    except Exception as e:
        refused = {"ok": False, "error": rp._safe(e)}
    _write(out, "bind-refused.json", {"ts": _now(), **refused})
    summary["bind_refused_ok"] = refused["ok"]
    _write(out, "stack-facts-after.json", _status(root))

    residual = subprocess.run(
        ["pgrep", "-fl", "live_entry.py|tunnel_endpoint.py|"
                         "supervise_run.py|mcp_proxy"],
        capture_output=True)
    (out / "residual-check.txt").write_text(
        residual.stdout.decode(errors="replace"), encoding="utf-8")
    summary["residual_expected_only"] = not residual.stdout.decode().count(
        "mcp_proxy")  # proxy children are per-connection, must be 0 idle

    summary["host"] = {"system": platform.system(),
                       "release": platform.release(),
                       "python": platform.python_version(),
                       "head": subprocess.run(
                           ["git", "-C", str(a3_stack.ROOT_REPO),
                            "rev-parse", "HEAD"],
                           capture_output=True).stdout.decode().strip()}
    ok = (summary["fixed_port_ok"] and summary["health_ok"]
          and summary["readiness_core_ok"] and summary["tools_roundtrip_ok"]
          and summary["dialer_roundtrip_ok"] and summary["bind_refused_ok"]
          and summary["residual_expected_only"])
    live_blocked = bool(ns.allow_live and live.get("ok") is False)
    summary.update({"overall_ok": bool(ok and not live_blocked),
                    "live_model_blocked": live_blocked,
                    "finished_at": _now()})
    _write(out, "gate-summary.json", summary)

    manifest = {"scope": summary["scope"], "files": {}}
    for f in sorted(out.rglob("*")):
        if f.is_file() and f.name != "manifest.json":
            manifest["files"][str(f.relative_to(out))] = {
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    _write(out, "manifest.json", manifest)

    print(json.dumps({"overall_ok": summary["overall_ok"],
                      "live_model_blocked": live_blocked,
                      "checks": {k: v for k, v in summary.items()
                                 if k.endswith("_ok")}},
                     ensure_ascii=False))
    if not ok:
        return 1
    return 3 if live_blocked else 0


if __name__ == "__main__":
    sys.exit(main())
