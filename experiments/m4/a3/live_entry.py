#!/usr/bin/env python3
"""M4-A3 live supervisor entry — the supervised child for the ChatGPT
round-trip test (manual start only).

This is the A3 harness child for `supervise_run.py run
--child-cmd ...`: the A2 fake default and its `--backend live` refusal
stay untouched (the launchd INSTALL approval has NOT happened); A3's
explicit, manual live round-trip is a separately directed scope (M4
brief, user-approved live model = SUT opencode/glm-5.3-flash-free).

Hard guards in this file:
  - refuses to start at all unless env M4_A3_LIVE=1 is set (explicit
    manual marker; no accidental or service-path startup);
  - --server-url must be an http:// loopback 127.0.0.1/localhost URL —
    anything else is refused before the backend is constructed;
  - the catalog carries EXACTLY the SUT model (D2: no substitution, no
    free/Go fallback; the model id is validated against the contract
    pattern here and by worker_start's schema at runtime);
  - binary fingerprint is probed at startup — pin/version mismatch
    aborts (exit 3) before any UDS is served;
  - profile surface is inspect-only; the approval channel stays
    unverified (challenge=null path), so a permission-hungry job lands
    in waiting_approval, never auto-approved.

M1 sources are unmodified — Supervisor/LiveBackend/ModelCatalog are used
as-is (same wiring style as P4/PI). stdout/stderr belong to the A2
wrapper's rotating journal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "src"))

import a3_stack  # noqa: E402
from hina_opencode_worker.supervisor import (  # noqa: E402
    ModelCatalog, Supervisor, SupervisorConfig)

LIVE_ENV = "M4_A3_LIVE"
_LOOPBACK_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", re.I)
_MODEL_RE = re.compile(r"^[^\s/]+/[^\s]+$")   # mcp-tools.json worker_start


def build_live_backend(binary: str, server_url: str, model: str,
                       probe_status: str):
    """LiveBackend + catalog. The probe() call enforces the M0 binary
    pin (sha256 + version) and raises ContractMismatch on drift — no
    model call, no account action happens here."""
    from hina_opencode_worker.supervisor import LiveBackend
    if not _LOOPBACK_RE.match(server_url or ""):
        raise ValueError(
            "server_url %r is not an http loopback URL — A3 refuses any"
            " non-loopback backend" % (server_url,))
    if not _MODEL_RE.match(model or ""):
        raise ValueError("model %r does not match the contract id pattern"
                         % (model,))
    if probe_status not in ("unverified", "help_only", "live_verified",
                            "unavailable"):
        raise ValueError("bad probe_status %r" % (probe_status,))
    # Startup and refresh both use the actual ProbeResult.sha256 field.
    from hina_opencode_worker import opencode_v2 as oc
    res = oc.probe(binary)          # pin-enforcing fingerprint (no LLM)
    info = {"version": res.version, "binary_sha256": res.sha256,
            "mapping_revision": "m4a3-live-entry"}
    backend = LiveBackend(binary=binary, server_url=server_url,
                          adapter=None)
    cat = ModelCatalog(profiles=("inspect",), gate="PASS", engine={
        "version": info["version"],
        "binary_sha256": info["binary_sha256"],
        "mapping_revision": info["mapping_revision"],
    }, capabilities={"inspect": True})
    # EXACTLY the SUT model — no other id exists in this catalog (D2).
    # Model selection does not establish billing classification.
    cat.add(model, "unknown", enabled=True, probe_status=probe_status)
    return backend, cat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--server-url", required=True,
                    help="http://127.0.0.1:<port> of the RUNNING "
                         "opencode serve (discovered read-only)")
    ap.add_argument("--binary",
                    default=os.path.expanduser("~/.opencode/bin/opencode"))
    ap.add_argument("--model", default=a3_stack.SUT_MODEL)
    ap.add_argument("--model-probe-status", default="unverified",
                    choices=("unverified", "help_only", "live_verified",
                             "unavailable"),
                    help="set live_verified ONLY after the A3 readiness "
                         "live tier passed for this exact id")
    ns = ap.parse_args(argv)

    if os.environ.get(LIVE_ENV) != "1":
        sys.stderr.write(
            "live_entry: REFUSED — env %s=1 is required (A3 live run is "
            "manual-start only; the service entry stays refused per "
            "M4-A2). exit 2\n" % LIVE_ENV)
        return 2

    try:
        backend, catalog = build_live_backend(ns.binary, ns.server_url,
                                              ns.model,
                                              ns.model_probe_status)
    except Exception as e:
        sys.stderr.write("live_entry: backend setup refused: %s\n" % e)
        return 3

    cfg = SupervisorConfig(root=os.path.abspath(ns.root),
                           profiles=("inspect",),
                           develop_enforcement_verified=False,
                           approval_channel_verified=False)
    try:
        sup = Supervisor(cfg, backend=backend, catalog=catalog)
    except Exception as e:
        sys.stderr.write("live_entry: supervisor start failed: %s\n" % e)
        return 3
    sock = sup.start_uds()

    pid_path = Path(cfg.run_dir) / "supervisor.pid"
    fd = os.open(str(pid_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    sys.stdout.write(json.dumps({
        "evt": "supervisor_up", "pid": os.getpid(), "root": cfg.root,
        "sock": sock, "backend": "live", "server_url": ns.server_url,
        "model": ns.model, "model_probe_status": ns.model_probe_status,
        "binary_version": catalog.engine.get("version"),
        "binary_sha256": catalog.engine.get("binary_sha256")}) + "\n")
    sys.stdout.flush()

    stop = threading.Event()

    def _on_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    rc = 0
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        try:
            sup.close()
        finally:
            try:
                if pid_path.exists() and \
                        pid_path.read_text().strip() == str(os.getpid()):
                    pid_path.unlink()
            except OSError:
                pass
        sys.stdout.write(json.dumps({"evt": "supervisor_stopped",
                                     "pid": os.getpid()}) + "\n")
        sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
