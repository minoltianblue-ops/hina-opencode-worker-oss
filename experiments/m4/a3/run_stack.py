#!/usr/bin/env python3
"""M4-A3 manual stack control — supervisor(live) + tunnel endpoint on a
FIXED localhost port.

  up       start the managed live run (A2 wrapper as the supervisor,
           child = live_entry.py) and tunnel_endpoint.py on
           127.0.0.1:8899, wait for READY+HEALTH, print stack facts
  status   wrapper READY check + endpoint info + pids (JSON, one line)
  down     SIGTERM the endpoint first, then the managed-run wrapper
           (operator stop — SIGTERM only, per the A2 runbook)
  fixture  create the synthetic inspect-only fixture repo the agreed
           test prompt reads (a committed CODE token; no user data)

Everything here is MANUAL start only (the brief's "manual-only"): no
launchd, no launchctl, no cokacremote/V1 changes. Processes are started
detached from this command so the operator's stop is always `down`
(SIGTERM), never a kill -9 of the supervisor journal path. The V1
`opencode serve` is only DISCOVERED (pgrep/lsof), never started.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import a3_stack  # noqa: E402

A2_DIR = a3_stack.ROOT_REPO / "experiments" / "m4" / "a2"
A1_DIR = a3_stack.ROOT_REPO / "experiments" / "m4" / "a1"
SUPERVISE = str(A2_DIR / "supervise_run.py")
ENDPOINT = str(A1_DIR / "tunnel_endpoint.py")
LIVE_ENTRY = str(HERE / "live_entry.py")
DIALER = str(HERE / "stdio_dialer.py")


def _paths(ns) -> dict:
    return a3_stack.root_paths(os.path.abspath(os.path.expanduser(ns.root)))


def _spawn_detached(argv: list[str], out_path: str, err_path: str,
                    env: dict) -> int:
    out_fh = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    err_fh = os.open(err_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    p = subprocess.Popen(argv, stdout=out_fh, stderr=err_fh, env=env,
                         cwd=str(a3_stack.ROOT_REPO), start_new_session=True,
                         close_fds=True)
    os.close(out_fh)
    os.close(err_fh)
    return p.pid


def _base_env(paths: dict) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = (str(a3_stack.ROOT_REPO / "src") + os.pathsep
                         + env.get("PYTHONPATH", ""))
    env.setdefault("M4_A3_LIVE", "1")
    return env


def cmd_up(ns) -> int:
    paths = _paths(ns)
    os.makedirs(paths["logs"], mode=0o700, exist_ok=True)
    # the pid files live in <root>/run — create it before spawning so a
    # crash can never orphan an untracked stack (the supervisor audits
    # the mode itself on start)
    os.makedirs(os.path.dirname(paths["sock"]), mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(paths["fixture_repo"]), mode=0o700,
                exist_ok=True)

    # already up? never double-start (the supervisor lock would also refuse)
    wpid = a3_stack.read_pid(paths["wrapper_pid"])
    epid = a3_stack.read_pid(paths["endpoint_pid"])
    if wpid and a3_stack.pid_alive(wpid):
        a3_stack.die("managed run wrapper pid=%s already alive — run "
                     "`down` first or use `status`" % wpid)
    if epid and a3_stack.pid_alive(epid):
        a3_stack.die("endpoint pid=%s already alive" % epid)
    # fixed port must be FREE or ours — never steal someone else's port
    probe = subprocess.run(["lsof", "-nP", "-iTCP:%d" % a3_stack.FIXED_PORT,
                            "-sTCP:LISTEN"], capture_output=True)
    if probe.returncode == 0 and probe.stdout.strip():
        a3_stack.die("fixed port %d already has a listener — NOT touching"
                     " it; inspect and free it first (no port stealing):\n%s"
                     % (a3_stack.FIXED_PORT,
                        probe.stdout.decode(errors="replace")[:400]))

    server_url = a3_stack.discover_server_url()
    has_pw = a3_stack.ensure_opencode_password_env()
    print("a3 up: backend server_url=%s opencode_password=%s"
          % (server_url, "inherited" if has_pw else "MISSING"),
          file=sys.stderr)

    env = _base_env(paths)
    child_cmd = " ".join(shlex.quote(x) for x in [
        sys.executable, LIVE_ENTRY, "--root", paths["root"],
        "--server-url", server_url, "--model", ns.model,
        "--model-probe-status", ns.model_probe_status])
    wrapper_pid = _spawn_detached(
        [sys.executable, SUPERVISE, "run", "--root", paths["root"],
         "--child-cmd", child_cmd],
        os.path.join(paths["logs"], "wrapper.out.log"),
        os.path.join(paths["logs"], "wrapper.err.log"), env)
    with open(paths["wrapper_pid"], "w") as fh:
        fh.write(str(wrapper_pid))

    # wait for supervisor READY (managed run + live entry pin probe)
    deadline = time.monotonic() + ns.ready_timeout
    ready = False
    last = None
    while time.monotonic() < deadline:
        r = subprocess.run([sys.executable, SUPERVISE, "status",
                            "--root", paths["root"]], capture_output=True,
                           env=env)
        last = r.stdout.decode(errors="replace").strip()
        if r.returncode == 0:
            ready = True
            break
        if not a3_stack.pid_alive(wrapper_pid):
            break
        time.sleep(0.5)
    if not ready:
        print("a3 up: supervisor NOT READY: %s" % last, file=sys.stderr)
        print("--- wrapper.err.log tail ---", file=sys.stderr)
        print(Path(os.path.join(paths["logs"],
                                "wrapper.err.log")).read_text(
            errors="replace")[-1500:], file=sys.stderr)
        return 1

    # endpoint on the FIXED port (never ephemeral; loopback enforced
    # inside tunnel_endpoint.py itself)
    endpoint_pid = _spawn_detached(
        [sys.executable, ENDPOINT, "--socket", paths["sock"],
         "--tools", a3_stack.TOOLS_DOC, "--bind", a3_stack.FIXED_HOST,
         "--port", str(a3_stack.FIXED_PORT),
         "--info-file", paths["endpoint_info"],
         "--proxy-log", os.path.join(paths["logs"], "mcp-proxy.log")],
        os.path.join(paths["logs"], "endpoint.out.log"),
        os.path.join(paths["logs"], "endpoint.err.log"), env)
    with open(paths["endpoint_pid"], "w") as fh:
        fh.write(str(endpoint_pid))

    deadline = time.monotonic() + 10.0
    info = None
    while time.monotonic() < deadline:
        try:
            info = json.loads(Path(paths["endpoint_info"]).read_text())
            if info.get("port") == a3_stack.FIXED_PORT:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    if not info or info.get("port") != a3_stack.FIXED_PORT:
        a3_stack.die("endpoint did not record fixed port %d"
                     % a3_stack.FIXED_PORT, 1)

    facts = {
        "fixed_endpoint": "http://%s:%d" % (a3_stack.FIXED_HOST,
                                            a3_stack.FIXED_PORT),
        "bound_host": info.get("bound_host"), "port": info.get("port"),
        "mcp_path": a3_stack.MCP_PATH,
        "dialer_command": "%s %s --port %d" % (shlex.quote(sys.executable),
                                               shlex.quote(DIALER),
                                               a3_stack.FIXED_PORT),
        "supervisor_socket": paths["sock"],
        "root": paths["root"], "fixture_repo": paths["fixture_repo"],
        "wrapper_pid": wrapper_pid, "endpoint_pid": endpoint_pid,
        "supervisor_pid": a3_stack.read_pid(paths["root"] + "/run/"
                                            "supervisor.pid"),
        "server_url": server_url, "model": ns.model,
        "model_probe_status": ns.model_probe_status,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps(facts, ensure_ascii=False, indent=1))
    return 0


def cmd_status(ns) -> int:
    paths = _paths(ns)
    env = _base_env(paths)
    out = {"ready": False}
    r = subprocess.run([sys.executable, SUPERVISE, "status",
                        "--root", paths["root"]], capture_output=True,
                       env=env)
    try:
        out["supervisor"] = json.loads(r.stdout.decode())
    except ValueError:
        out["supervisor"] = {"raw": r.stdout.decode(errors="replace")[:300]}
    out["supervisor_exit"] = r.returncode
    try:
        info = json.loads(Path(paths["endpoint_info"]).read_text())
        out["endpoint"] = {"host": info.get("bound_host"),
                           "port": info.get("port"),
                           "pid_alive": a3_stack.pid_alive(
                               int(info["pid"]))}
    except (OSError, ValueError, KeyError):
        out["endpoint"] = None
    wpid = a3_stack.read_pid(paths["wrapper_pid"])
    out["wrapper_pid"] = wpid
    out["wrapper_alive"] = bool(wpid and a3_stack.pid_alive(wpid))
    out["ready"] = bool(r.returncode == 0 and out["endpoint"]
                        and out["endpoint"].get("port")
                        == a3_stack.FIXED_PORT
                        and out["endpoint"]["pid_alive"])
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ready"] else 1


def _term(pid: int, label: str) -> None:
    if not pid or not a3_stack.pid_alive(pid):
        print("a3 down: %s not running" % label, file=sys.stderr)
        return
    print("a3 down: SIGTERM %s pid=%d" % (label, pid), file=sys.stderr)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15.0
    while a3_stack.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.3)
    if a3_stack.pid_alive(pid):
        print("a3 down: %s still alive after 15s — leaving it (no kill -9"
              " from this harness; operator decides)" % label,
              file=sys.stderr)


def cmd_down(ns) -> int:
    paths = _paths(ns)
    _term(a3_stack.read_pid(paths["endpoint_pid"]), "endpoint")
    _term(a3_stack.read_pid(paths["wrapper_pid"]), "managed-run wrapper")
    for p in (paths["endpoint_pid"], paths["wrapper_pid"]):
        try:
            os.unlink(p)
        except OSError:
            pass
    leftover = subprocess.run(
        ["pgrep", "-fl", "live_entry.py|tunnel_endpoint.py|supervise_run"],
        capture_output=True)
    print(leftover.stdout.decode() or "residual: none")
    return 0


def cmd_fixture(ns) -> int:
    """Synthetic, inspect-only fixture repo: a committed CODE token the
    test prompt asks the model to quote. Lives under the supervisor root
    (0700); contains NO user data (M0 fixture style)."""
    paths = _paths(ns)
    repo = Path(paths["fixture_repo"])
    marker = repo / "FIXTURE.md"
    if marker.exists():
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True)
        print(json.dumps({"fixture_repo": str(repo), "exists": True,
                          "head": head.stdout.decode().strip()[:12]}))
        return 0
    token = "HINA-A3-%d" % (int(time.time()) % 100000000)
    os.makedirs(str(repo), mode=0o700, exist_ok=True)
    marker.write_text(
        "# M4-A3 synthetic fixture\n\n"
        "This repo is a synthetic test fixture for the M4-A3 ChatGPT "
        "round-trip (inspect profile, read-only task). No user data.\n\n"
        "CODE: %s\n" % token, encoding="utf-8")
    for argv in (["git", "-C", str(repo), "init", "-q", "-b", "main"],
                 ["git", "-C", str(repo), "config",
                  "user.email", "fixture@localhost"],
                 ["git", "-C", str(repo), "config", "user.name",
                  "M4-A3 synthetic fixture"],
                 ["git", "-C", str(repo), "add", "FIXTURE.md"],
                 ["git", "-C", str(repo), "commit", "-q", "-m",
                  "synthetic fixture: CODE token for A3 round-trip"]):
        p = subprocess.run(argv, capture_output=True)
        if p.returncode != 0:
            a3_stack.die("fixture git failed: %s: %s"
                         % (argv, p.stderr.decode(errors="replace")[:200]))
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True)
    print(json.dumps({"fixture_repo": str(repo), "created": True,
                      "head": head.stdout.decode().strip()[:12]}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=a3_stack.DEFAULT_ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--model", default=a3_stack.SUT_MODEL)
    u.add_argument("--model-probe-status", default="unverified",
                   choices=("unverified", "help_only", "live_verified",
                            "unavailable"))
    u.add_argument("--ready-timeout", type=float, default=30.0)
    u.set_defaults(fn=cmd_up)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    sub.add_parser("fixture").set_defaults(fn=cmd_fixture)
    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
