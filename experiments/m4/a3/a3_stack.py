#!/usr/bin/env python3
"""M4-A3 shared constants and read-only discovery helpers.

Fixed addressing (A3 brief): the tunnel-local endpoint listens on the
FIXED loopback endpoint below — never an ephemeral port, never a
non-loopback bind. These constants are the single source of truth for
the values recorded in HANDOFF-A3 and CHATGPT-SETUP.md.

Discovery here is deliberately read-only (pgrep/lsof/launchctl): the
A3 run never starts, restarts or touches the V1 `opencode serve`
runtime or cokacremote — it only observes where they already are.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_REPO = HERE.parents[2]

# --- fixed localhost endpoint (recorded in HANDOFF-A3/CHATGPT-SETUP.md) ---
FIXED_HOST = "127.0.0.1"
FIXED_PORT = 8899                    # manual-fixed; NOT ephemeral
MCP_PATH = "/mcp"                    # connector-side path convention
# SUT is USER-designated every time (coordinator decision 2026-10-20,
# dispatch ctx_207ca37eaf95): the original opencode/glm-5.3-flash-free is
# provider-403 FreeTierError-blocked on this network and the insistence
# on it was withdrawn. Never auto-switch; this constant only records the
# current user selection.
SUT_MODEL = "opencode-go/muse-spark-1.3-contributor"

# supervisor root: the documented product runtime path (MCP.md §1).
# Manual start only — this run installs no service (launchd stays a draft).
DEFAULT_ROOT = os.path.expanduser(
    "~/Library/Application Support/HinaOpenCodeWorker")

TOOLS_DOC = str(ROOT_REPO / "docs" / "contracts" / "mcp-tools.json")


def root_paths(root: str) -> dict:
    return {
        "root": root,
        "sock": os.path.join(root, "run", "supervisor.sock"),
        "pid": os.path.join(root, "run", "supervisor.pid"),
        "endpoint_info": os.path.join(root, "run", "endpoint-info.json"),
        "endpoint_pid": os.path.join(root, "run", "endpoint.pid"),
        "wrapper_pid": os.path.join(root, "run", "wrapper.pid"),
        "logs": os.path.join(root, "logs"),
        "fixture_repo": os.path.join(root, "fixture", "a3-repo"),
    }


def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=timeout,
                          check=False)


def discover_serve_pid() -> int | None:
    """PID of the already-running `opencode serve` (V1 app runtime).
    Read-only pgrep; never a signal, never a start."""
    p = _run(["pgrep", "-f", "opencode serve"])
    pids = [int(x) for x in p.stdout.decode().split() if x.strip().isdigit()]
    return pids[0] if pids else None


def discover_server_url() -> str:
    """http://127.0.0.1:<port> of the live `opencode serve` listener.

    The V1 runtime picks its own loopback port; A3 observes it with lsof
    (read-only) instead of hardcoding a port that changes on the user's
    next app restart. Loopback-only listener is a hard requirement."""
    pid = discover_serve_pid()
    if pid is None:
        raise RuntimeError("no live `opencode serve` process found — the "
                           "worker live backend needs the already-running "
                           "V1 runtime; A3 never starts it")
    p = _run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"])
    ports = []
    for line in p.stdout.decode().splitlines()[1:]:
        m = re.search(r"127\.0\.0\.1:(\d+)\s*\(LISTEN\)", line)
        if m:
            ports.append(int(m.group(1)))
    if not ports:
        raise RuntimeError("opencode serve pid %s has no loopback LISTEN "
                           "socket (observed via lsof) — refusing to guess"
                           % pid)
    if len(set(ports)) > 1:
        raise RuntimeError("opencode serve pid %s listens on multiple"
                           " loopback ports %s — manual inspection needed"
                           % (pid, sorted(set(ports))))
    return "http://%s:%d" % (FIXED_HOST, ports[0])


def ensure_opencode_password_env() -> bool:
    """SessionApiTransport reads OPENCODE_PASSWORD from the environment
    at call time (opencode_v2 docstring). Resolution order, values never
    printed or logged: shell env -> launchctl user session -> the
    resident service's own config (~/.config/opencode/service.json
    "password", read-only — same secret the V1 runtime uses)."""
    if os.environ.get("OPENCODE_PASSWORD"):
        return True
    p = _run(["launchctl", "getenv", "OPENCODE_PASSWORD"])
    val = p.stdout.decode().strip()
    if not val:
        try:
            import json as _json
            with open(os.path.expanduser(
                    "~/.config/opencode/service.json")) as fh:
                val = str(_json.load(fh).get("password") or "")
        except (OSError, ValueError):
            val = ""
    if val:
        os.environ["OPENCODE_PASSWORD"] = val
        return True
    return False


def check_binaries() -> dict:
    out = {}
    for name in ("pgrep", "lsof", "launchctl", "git"):
        out[name] = shutil.which(name)
    return out


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write("a3: %s\n" % msg)
    raise SystemExit(code)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None
