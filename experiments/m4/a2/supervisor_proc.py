#!/usr/bin/env python3
"""M4-A2 supervised supervisor process — the child of the managed run.

Starts a real Supervisor (M1 P4, unmodified) on <root>, serves the UDS,
runs until SIGTERM/SIGINT (clean close), and exits 0. Writes
<root>/run/supervisor.pid (0600; a crash leaves it stale, the next
start overwrites) and one `supervisor_up` JSON line on stdout.
stdout/stderr are captured and retained by supervise_run.py.

Backend: --backend fake (this package's default for evidence and manual
start). ScriptedBackend below — scripted waits only, ZERO real prompts,
ZERO model calls (D2; the live_model tier stays blocked). --backend live
is REFUSED here: the live service entry (LiveBackend wiring, binary pin,
tunnel handoff) is finalized together with the launchd INSTALL approval,
which is a separate user decision. This file is the manual-run harness,
not the installed service.

Fake script (--fake-script path): JSON list of per-start action lists;
each action is {"action":"sleep","seconds":N} or
{"action":"complete_after","seconds":N,"reason":str}. Starts beyond the
script reuse the LAST entry. No script -> every start sleeps forever
(the crash-restart shape).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from hina_opencode_worker.events import VERDICT_COMPLETED  # noqa: E402
from hina_opencode_worker.opencode_v2 import CancelResult  # noqa: E402
from hina_opencode_worker.supervisor import (  # noqa: E402
    AttemptOutcome, BackendRun, ModelCatalog, Supervisor, SupervisorBackend,
    SupervisorConfig)


class ScriptedBackend(SupervisorBackend):
    """Fake backend for the managed run: scripted waits, no processes,
    no network, no model. Mirrors tests/_p4_support.FakeBackend's shape
    but is self-contained (no test dependency in the runtime path)."""

    def __init__(self, script: list[list[dict]]):
        self._script = script or [[{"action": "sleep", "seconds": 1e12}]]
        self.runs: dict[str, list] = {}
        self.started: list[dict] = []
        self._n = 0
        self._lock = threading.Lock()

    def probe(self) -> dict:
        return {"version": "m4a2-fake-1.0", "binary_sha256": "f" * 64,
                "mapping_revision": "m4a2-fake"}

    def available(self, model: str) -> bool:
        return True

    def start(self, *, job_id, attempt_id, model, cwd, prompt) -> BackendRun:
        with self._lock:
            idx = self._n
            self._n += 1
        actions = list(self._script[min(idx, len(self._script) - 1)])
        sid = f"m4a2-fake-sess-{idx}"
        self.runs[sid] = actions
        self.started.append({"job_id": job_id, "model": model})
        return BackendRun(sid)

    @staticmethod
    def _sleep(seconds: float, cancel: threading.Event) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end and not cancel.is_set():
            time.sleep(min(0.02, end - time.monotonic()))

    def wait(self, run, *, cancel, deadline_s, on_permission):
        acts = self.runs.setdefault(run.session_id, [])
        if acts:
            a = acts[0]
            if a.get("action") == "sleep":
                acts.pop(0)
                self._sleep(min(a["seconds"], deadline_s), cancel)
                return None
            if a.get("action") == "complete_after":
                until = getattr(run, "_a2_until", None)
                if until is None:
                    run._a2_until = time.monotonic() + a["seconds"]
                    self._sleep(min(a["seconds"], deadline_s), cancel)
                    return None
                if time.monotonic() >= until and not cancel.is_set():
                    acts.pop(0)
                    return AttemptOutcome(
                        VERDICT_COMPLETED,
                        a.get("reason", "m4a2 scripted completion"),
                        usage="unknown", session_id=run.session_id,
                        raw_stdout=b'{"type":"text"}\n', exit_code=0)
                self._sleep(min(until - time.monotonic(), deadline_s),
                            cancel)
                return None
        # idle: honour the slice, never longer, never holding a lock
        self._sleep(deadline_s, cancel)
        return None

    def reconcile(self, session_id: str, timeout_s: float) -> str:
        return "idle"

    def prompt(self, session_id: str, text: str, delivery: str):
        raise NotImplementedError("m4a2 fake backend: no delivery")

    def pending_permissions(self, session_id):
        return []

    def reply_permission(self, session_id, request_id, decision) -> bool:
        raise NotImplementedError("m4a2 fake backend: no approvals")

    def cancel(self, run) -> CancelResult:
        self.runs.setdefault(run.session_id, []).clear()
        return CancelResult(True, "m4a2 fake: steps cleared")


def build_catalog() -> ModelCatalog:
    cat = ModelCatalog(profiles=("inspect", "develop"), gate="PASS",
                       capabilities={"develop": True})
    cat.add("fake/free", "free", probe_status="live_verified")
    return cat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--backend", default="fake", choices=("fake", "live"))
    ap.add_argument("--fake-script", default=None)
    ap.add_argument("--banner-bytes", type=int, default=1536,
                    help="operational banner padding on stdout (log-"
                         "retention behaviour is observable without it)")
    ns = ap.parse_args(argv)

    if ns.backend == "live":
        sys.stderr.write(
            "supervisor_proc: --backend live is refused at M4-A2: the "
            "live service entry and any live model call require separate "
            "user approval (D2 / M4 install approval). exit 2\n")
        return 2

    script = None
    if ns.fake_script:
        script = json.loads(Path(ns.fake_script).read_text(encoding="utf-8"))
        if not isinstance(script, list) or not all(
                isinstance(run, list) for run in script):
            sys.stderr.write("supervisor_proc: --fake-script must be a "
                             "JSON list of action lists\n")
            return 2

    backend = ScriptedBackend(script or [])
    cfg = SupervisorConfig(root=os.path.abspath(ns.root),
                           develop_enforcement_verified=True,
                           approval_channel_verified=False)
    sup = Supervisor(cfg, backend=backend, catalog=build_catalog())
    sock = sup.start_uds()

    pid_path = Path(cfg.run_dir) / "supervisor.pid"
    fd = os.open(str(pid_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    banner = ("supervisor_proc: fake-backend supervised run pid=%d root=%s\n"
              % (os.getpid(), cfg.root))
    sys.stdout.write(banner)
    pad = "m4a2 operational banner padding (no user data)\n"
    while len(banner) < ns.banner_bytes:
        sys.stdout.write(pad)
        banner += pad
    sys.stdout.write(json.dumps({"evt": "supervisor_up", "pid": os.getpid(),
                                 "root": cfg.root, "sock": sock}) + "\n")
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
