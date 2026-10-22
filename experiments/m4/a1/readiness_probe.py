#!/usr/bin/env python3
"""M4-A1 probe: health vs readiness split checks for the MCP proxy stack.

PLAN §4 topology: ChatGPT -> Tunnel -> stdio MCP proxy -> supervisor.
This probe talks to the LOCALHOST-ONLY tunnel-local endpoint
(tunnel_endpoint.py) — never a public address.

health ("is the process alive?" — NO backend dependency):
  endpoint  tunnel endpoint process alive (pid liveness)
  transport TCP connect to the recorded loopback endpoint
  proxy     initialize + ping round-trip answered by the proxy.
            Both are answered by the proxy WITHOUT the supervisor
            (mcp_proxy.py handle()), so health can be OK while the
            backend is down — that is the demonstrated split.

readiness ("can work actually reach the backend+model?"), tiered:
  tunnel      the full health chain (needed for readiness too)
  supervisor  tools/call worker_catalog through endpoint->proxy->UDS;
              a contract envelope with no error == backend reachable
  binary_pin  OpenCode binary fingerprint + --version match the signed
              gate pins (OPENCODE_V2.md §5, OBSERVED_HELP). Read-only;
              no model call, no account action.
  model_policy  at least one enabled+available model in the catalog;
              billing class is reported, NEVER inferred or auto-selected
              (D2: no automatic fallback, free or otherwise).
  live_model  LIVE model reachability per the observed CLI-ARGV contract
              (`run --model M --format json`) is DORMANT: it needs a
              separate user approval for an account-touching probe. It
              only runs with --allow-live AND env
              M4_A1_LIVE_MODEL_OK=<exact catalog-approved model id>;
              otherwise the check records BLOCKED_NO_APPROVAL and does
              NOT block the readiness verdict.

Blocked-until-ready proof: `gate` loops readiness until ok, appending
every attempt (redacted) to --log-file. Exit code 0 ready / 1 not.

stdout stays one machine-readable JSON document; the JSONL log is the
audit trail. All logged strings pass through artifacts.redact_text.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT / "src"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hina_opencode_worker.artifacts import redact_text  # noqa: E402

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18",
                         "capabilities": {},
                         "clientInfo": {"name": "m4a1-readiness-probe",
                                        "version": "0.1"}}}
PING = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
CATALOG_CALL = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "worker_catalog", "arguments": {}}}
CONNECT_TIMEOUT_S = 5.0


def _safe(s) -> str:
    text, _ = redact_text(str(s))
    return text[:400]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + \
        ".%03dZ" % (int(time.time() * 1000) % 1000)


# ------------------------------------------------------------------ wire

def _rpc_roundtrip(host: str, port: int, msgs: list[dict],
                   *, expect_ids) -> list[dict]:
    conn = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
    replies = []
    try:
        buf = b""
        for m in msgs:
            conn.sendall((json.dumps(m) + "\n").encode("utf-8"))
        want = set(expect_ids)
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while want and time.monotonic() < deadline:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                doc = json.loads(line.decode("utf-8"))
                if doc.get("id") in want:
                    want.discard(doc["id"])
                    replies.append(doc)
    finally:
        conn.close()
    if want:
        raise TimeoutError("no reply for ids %s" % sorted(want))
    return sorted(replies, key=lambda d: d["id"])


def load_info(path: str) -> dict:
    info = json.loads(Path(path).read_text(encoding="utf-8"))
    if info.get("kind") != "m4a1-tunnel-endpoint":
        raise ValueError("not an m4a1 tunnel endpoint info file")
    return info


# ---------------------------------------------------------------- checks

def check_endpoint(info: dict) -> dict:
    try:
        os.kill(int(info["pid"]), 0)
        return {"ok": True, "detail": _safe("pid %s alive" % info["pid"])}
    except OSError as e:
        return {"ok": False, "detail": _safe("pid dead: %s" % e)}


def check_health(info: dict) -> dict:
    """health == process alive + proxy answers. Deliberately touches no
    backend: ping is served by the proxy alone (health != readiness)."""
    host = info.get("bound_host")
    if host != "127.0.0.1":
        return {"ok": False, "detail": _safe("endpoint not loopback: %s"
                                             % host)}
    ep = check_endpoint(info)
    transport = {"ok": False, "detail": "not attempted"}
    proxy = {"ok": False, "detail": "not attempted"}
    try:
        replies = _rpc_roundtrip(host, int(info["port"]),
                                 [INITIALIZE, PING], expect_ids=[1, 2])
        by_id = {d["id"]: d for d in replies}
        init = by_id.get(1, {})
        proxy = {"ok": (init.get("result", {}).get("serverInfo", {})
                        .get("name") == "hina-opencode-worker-proxy"
                        and 2 in by_id and "error" not in by_id[2]),
                 "detail": _safe("initialize+ping answered")}
        transport = {"ok": True, "detail": _safe("connected %s:%s"
                                                 % (host, info["port"]))}
    except Exception as e:
        detail = _safe("%s: %s" % (type(e).__name__, e))
        if transport["ok"] is False and "connected" not in \
                str(transport["detail"]):
            transport = {"ok": False, "detail": detail}
        proxy = {"ok": False, "detail": detail}
    return {"ok": bool(ep["ok"] and transport["ok"] and proxy["ok"]),
            "checks": {"endpoint": ep, "transport": transport,
                       "proxy": proxy}}


def _catalog_through(info: dict) -> dict:
    """Full chain: endpoint -> proxy -> supervisor UDS."""
    replies = _rpc_roundtrip(info["bound_host"], int(info["port"]),
                             [INITIALIZE, {"jsonrpc": "2.0",
                                           "method":
                                           "notifications/initialized"},
                              CATALOG_CALL], expect_ids=[1, 3])
    by_id = {d["id"]: d for d in replies}
    call = by_id.get(3, {})
    if "error" in call:
        raise RuntimeError("tools/call failed: %s"
                           % call["error"].get("message"))
    env = call.get("result", {}).get("structuredContent") or {}
    return env


def check_readiness(info: dict, *, binary: str | None,
                    allow_live: bool, live_out: str | None) -> dict:
    from hina_opencode_worker import opencode_v2 as oc

    checks = {}
    checks["tunnel"] = check_health(info)

    try:
        env = _catalog_through(info)
        ok = (isinstance(env, dict)
              and env.get("contract_version") == "0.3"
              and env.get("tool") == "worker_catalog"
              and "error" not in env
              and isinstance(env.get("data", {}).get("models"), list))
        if not ok and isinstance(env, dict) and "error" in env:
            det = _safe("supervisor_unreachable (envelope error %s)"
                        % (env.get("error") or {}).get("code"))
        else:
            det = _safe("envelope tool=%s" % env.get("tool"))
        checks["supervisor"] = {"ok": ok, "detail": det}
    except Exception as e:
        checks["supervisor"] = {"ok": False,
                                "detail": _safe("supervisor_unreachable: %s"
                                                % e)}
        env = {}

    models = (env.get("data") or {}).get("models") or []
    ready_models = [m for m in models
                    if m.get("enabled")
                    and m.get("probe_status") != "unavailable"]
    if ready_models:
        mp_detail = _safe("catalog ready models=%s" % [
            {"id": m["id"], "billing": m["billing_class"],
             "probe_status": m["probe_status"]}
            for m in ready_models][:8])
    else:
        mp_detail = _safe("no enabled available model in catalog")
    checks["model_policy"] = {"ok": bool(ready_models),
                              "detail": mp_detail}

    bp = {"ok": False, "detail": "binary not probed"}
    if binary:
        try:
            # probe() itself enforces the M0 pins (sha256 + --version) and
            # raises ContractMismatch on any deviation — read-only, no
            # launch, no account action.
            res = oc.probe(binary)
            bp = {"ok": True,
                  "detail": _safe("version=%s pinned=%s sha_match=True "
                                  "sha=%s..." % (res.version,
                                                 oc.PINNED_CLI_VERSION,
                                                 res.sha256[:16]))}
        except Exception as e:
            bp = {"ok": False, "detail": _safe("CONTRACT_MISMATCH: %s" % e)}
    checks["binary_pin"] = bp

    lm = {"ok": None, "status": "BLOCKED_NO_APPROVAL",
          "detail": _safe("live model probe needs separate user approval "
                          "(--allow-live + M4_A1_LIVE_MODEL_OK); never "
                          "auto-selected, never blocks readiness")}
    if allow_live:
        chosen = os.environ.get("M4_A1_LIVE_MODEL_OK", "")
        if not chosen or chosen not in [m["id"] for m in ready_models]:
            lm = {"ok": False, "status": "MODEL_NOT_ALLOWED",
                  "detail": _safe("env model %r is not a catalog-approved "
                                  "available id" % chosen)}
        elif not binary:
            lm = {"ok": False, "status": "BINARY_MISSING",
                  "detail": _safe("live tier requested but no pinned "
                                  "binary given; refusing a partial run")}
        else:
            lm = _live_model_probe(binary, chosen, live_out)
    checks["live_model"] = lm

    core = ("tunnel", "supervisor", "binary_pin", "model_policy")
    ok = all(checks[k]["ok"] for k in core)
    if allow_live and lm["ok"] is False:
        ok = False
    return {"ok": ok, "checks": checks}


def _live_model_probe(binary: str, model: str,
                      live_out: str | None) -> dict:
    """One minimal synthetic prompt over the OBSERVED CLI-ARGV contract
    (OPENCODE_V2.md §1). DORMANT unless explicitly approved; never
    chooses a model by itself (D2)."""
    cmd = [binary, "run", "--model", model, "--format", "json",
           "--title", "hina-m4a1-readiness",
           "Readiness probe. Reply with exactly: ok"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        return {"ok": False, "status": "LIVE_PROBE_ERROR",
                "detail": _safe(e)}
    raw = p.stdout.decode("utf-8", "replace")
    if live_out:
        Path(live_out).write_text(_safe(raw), encoding="utf-8")
    saw_event = any(l.strip().startswith("{")
                    for l in raw.splitlines()[-5:])
    return {"ok": p.returncode == 0 and saw_event,
            "status": "LIVE_PROBE_RAN",
            "detail": _safe("exit=%s model=%s events_seen=%s"
                            % (p.returncode, model, saw_event))}


# ------------------------------------------------------------------ CLI

def _append_log(path, entry: dict) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("health", "readiness", "gate"))
    ap.add_argument("--info-file", required=True)
    ap.add_argument("--binary", default=os.environ.get("OPENCODE_BIN"),
                    help="opencode binary for the fingerprint pin")
    ap.add_argument("--allow-live", action="store_true",
                    help="dormant live model tier; needs env "
                         "M4_A1_LIVE_MODEL_OK with a catalog-approved id")
    ap.add_argument("--live-out", default=None)
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="gate mode: max seconds to keep blocking")
    ap.add_argument("--interval", type=float, default=0.5)
    ns = ap.parse_args(argv)

    info = load_info(ns.info_file)

    if ns.mode == "health":
        res = check_health(info)
        verdict = "HEALTHY" if res["ok"] else "UNHEALTHY"
        out = {"ts": _now(), "probe": "health", "verdict": verdict,
               "checks": res["checks"]}
        _append_log(ns.log_file, out)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    def attempt(n):
        res = check_readiness(info, binary=ns.binary,
                              allow_live=ns.allow_live,
                              live_out=ns.live_out)
        return {"ts": _now(), "probe": "readiness", "attempt": n,
                "verdict": "READY" if res["ok"] else "NOT_READY",
                "checks": res["checks"]}

    if ns.mode == "readiness":
        out = attempt(1)
        _append_log(ns.log_file, out)
        print(json.dumps(out, ensure_ascii=False, indent=0))
        return 0 if out["verdict"] == "READY" else 1

    # gate: blocked-until-ready. Every attempt is logged, including the
    # blocked ones — that log is the evidence the gate demanded.
    start = time.monotonic()
    n = 0
    last = None
    while True:
        n += 1
        last = attempt(n)
        _append_log(ns.log_file, last)
        if last["verdict"] == "READY" or time.monotonic() - start \
                > ns.timeout:
            break
        time.sleep(ns.interval)
    summary = {"ts": _now(), "probe": "gate", "attempts": n,
               "blocked_s": round(time.monotonic() - start, 3),
               "verdict": last["verdict"]}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if last["verdict"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
