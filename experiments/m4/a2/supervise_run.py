#!/usr/bin/env python3
"""M4-A2 managed supervisor run — manual start, crash restart, logs.

Subcommands
  run     Managed run of supervisor_proc.py under <root>: start manually,
          restart the child on unexpected exit (journal is untouched —
          the supervisor's SQLite store under <root>/db survives; WAL +
          synchronous=FULL, restart reconcile per STORAGE.md §3 ④),
          forward SIGTERM/SIGINT for a clean operator stop (no restart),
          and retain the child's stdout/stderr with size-based rotation.
          Crash-storm protection: after --max-fast consecutive crashes
          with uptime < --min-up-s the wrapper gives up and exits 70 so
          an outer supervisor (launchd KeepAlive/ThrottleInterval, draft
          plist NOT installed) throttles instead of a hot loop.
  viewer  Observe-only terminal viewer wiring: exec's
          hina_opencode_worker.viewer --socket <root>/run/supervisor.sock
          (read-only client; detaching the viewer never touches the
          job — T15). No injection surface exists in this path.
  status  One viewer.snapshot round trip -> READY / NOT_READY. Exit 0/1.

Before EVERY child start the wrapper clears a stale supervisor.sock,
but only while holding the supervisor flock (no live supervisor) and
only after a refused connect proves no listener — the supervisor itself
refuses to unlink an unknown socket, so this lock-verified cleanup is
the managed run's job (documented in HANDOFF-A2).

Python 3.12 stdlib only. --child-cmd is a test hook only (dummy child);
it is never used by the runbook or the plist draft.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hina_opencode_worker import viewer as viewer_mod  # noqa: E402


class Rotator:
    """Size-based retention: <base>.log plus at most keep-1 numbered
    backups. The write path checks size BEFORE appending, so a file may
    exceed max_bytes by one line, never by a run."""

    def __init__(self, logs_dir: Path, base: str, max_bytes: int,
                 keep: int):
        self.dir, self.base = logs_dir, base
        self.max_bytes, self.keep = max_bytes, max(1, keep)
        # create <root> and <root>/logs each at 0700 (a pre-existing
        # root keeps its own mode; the supervisor audits its own dirs)
        os.makedirs(str(self.dir), mode=0o700, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, n: int) -> Path:
        return self.dir / (self.base if n == 0 else f"{self.base}.{n}")

    def _rotate(self) -> None:
        if self.keep == 1:
            with open(self._path(0), "w"):
                pass
            return
        for i in range(self.keep - 2, 0, -1):
            src, dst = self._path(i), self._path(i + 1)
            if src.exists():
                src.replace(dst)
        cur = self._path(0)
        if cur.exists():
            cur.replace(self._path(1))

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self._lock:
            try:
                if self._path(0).stat().st_size + len(data) > self.max_bytes:
                    self._rotate()
            except FileNotFoundError:
                pass
            fd = os.open(self._path(0),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())

    def files(self) -> list[Path]:
        return sorted(p for p in self.dir.glob(f"{self.base}*") if p.exists())


def clear_stale_socket(root: Path) -> tuple[bool, str]:
    """(ok, reason). Safe to call before launching the child: touches the
    socket only while the flock is FREE (no live supervisor holds it) and
    only when nothing is listening on it."""
    run_dir, sock = root / "run", root / "run" / "supervisor.sock"
    if not run_dir.is_dir():
        return True, "no-run-dir"
    try:
        fd = os.open(str(run_dir / "supervisor.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True, "no-lock-file"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True, "lock-held"      # live supervisor; child
                # will hit AlreadyRunningError with its own clean message
            raise
        try:
            if sock.exists():
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(1.0)
                try:
                    probe.connect(str(sock))
                    return False, ("socket live but lock free — manual"
                                   " inspection, refusing to unlink")
                except OSError:
                    sock.unlink()
                    return True, "stale-socket-cleared"
            return True, "nothing-to-clear"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class ManagedRun:
    def __init__(self, ns):
        self.root = Path(ns.root).resolve()
        self.logs = Path(ns.logs).resolve() if ns.logs \
            else self.root / "logs"
        self.out = Rotator(self.logs, "supervisor.out.log",
                           ns.log_max_bytes, ns.log_keep)
        self.events = Rotator(self.logs, "run-events.log",
                              ns.log_max_bytes, ns.log_keep)
        self.child_cmd = ns.child_cmd
        self.fake_script = getattr(ns, "fake_script", None)
        self.min_up_s, self.max_fast = ns.min_up_s, ns.max_fast
        self.backoff_base, self.backoff_cap = ns.backoff_base, ns.backoff_cap
        self.kill_grace = ns.kill_grace
        self._child: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._attempt = 0
        self._fast = 0

    def _ev(self, msg: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        line = f"[{stamp}] {msg}\n"
        self.events.write(line)
        sys.stderr.write(line)
        sys.stderr.flush()

    def _pump(self, pipe) -> None:
        for line in iter(pipe.readline, ""):
            if line:
                self.out.write(line)
        try:
            pipe.close()
        except OSError:
            pass

    def _spawn(self) -> subprocess.Popen:
        if self.child_cmd:
            cmd = shlex.split(self.child_cmd)
        else:
            cmd = [sys.executable, str(HERE / "supervisor_proc.py"),
                   "--root", str(self.root)]
            if self.fake_script:
                cmd += ["--fake-script", self.fake_script]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + \
            env.get("PYTHONPATH", "")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1024, env=env, cwd=str(ROOT))
        threading.Thread(target=self._pump, args=(proc.stdout,),
                         daemon=True).start()
        return proc

    def _term_child(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self.kill_grace)
                return
            except subprocess.TimeoutExpired:
                self._ev(f"child pid={proc.pid} ignored SIGTERM;"
                         " SIGKILL")
                proc.kill()
                proc.wait(timeout=5)

    def run(self) -> int:
        self._ev(f"wrapper_start pid={os.getpid()} root={self.root}")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._stop.set())
        while not self._stop.is_set():
            ok, why = clear_stale_socket(self.root)
            if not ok:
                self._ev(f"cannot launch child: {why}")
                return 3
            if why == "lock-held":
                self._ev("another supervisor holds the lock — refusing"
                         " to start a second managed run")
                return 3
            self._attempt += 1
            t0 = time.monotonic()
            self._child = self._spawn()
            self._ev(f"child_start attempt={self._attempt}"
                     f" pid={self._child.pid}")
            while not self._stop.is_set():
                if self._child.poll() is not None:
                    break
                time.sleep(0.1)
            if self._stop.is_set():
                self._term_child(self._child)
                # the supervisor's close() does not unlink its own socket
                # (M1 behaviour); lock-verified cleanup leaves <root>/run
                # tidy on a clean operator stop
                ok, why = clear_stale_socket(self.root)
                self._ev(f"operator_stop child pid={self._child.pid}"
                         f" terminated (socket cleanup: {why});"
                         " managed run ends")
                return 0
            rc = self._child.returncode
            up = time.monotonic() - t0
            self._ev(f"child_exit rc={rc} up_s={up:.2f}"
                     f" attempt={self._attempt}")
            self._fast = self._fast + 1 if up < self.min_up_s else 0
            if self._fast >= self.max_fast:
                self._ev(f"give_up after {self._fast} consecutive fast"
                         f" crashes (< {self.min_up_s}s each); exiting 70"
                         " for the outer supervisor to throttle")
                return 70
            delay = min(self.backoff_base * (2 ** (self._fast - 1))
                        if self._fast else self.backoff_base,
                        self.backoff_cap)
            self._ev(f"restart in {delay:.2f}s"
                     f" (consecutive_fast={self._fast})")
            end = time.monotonic() + delay
            while not self._stop.is_set() and time.monotonic() < end:
                time.sleep(0.1)
        return 0


def cmd_run(ns) -> int:
    return ManagedRun(ns).run()


def cmd_viewer(ns) -> int:
    sock = Path(ns.root).resolve() / "run" / "supervisor.sock"
    extra: list[str] = []
    if ns.watch:
        extra.append("--watch")
    if ns.interval is not None:
        extra += ["--interval", str(ns.interval)]
    if ns.json:
        extra.append("--json")
    argv = [sys.executable, "-m", "hina_opencode_worker.viewer",
            "--socket", str(sock)] + extra
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + \
        env.get("PYTHONPATH", "")
    os.execvpe(argv[0], argv, env)  # never returns; no extra process


def cmd_status(ns) -> int:
    sock = str(Path(ns.root).resolve() / "run" / "supervisor.sock")
    out: dict = {"ready": False}
    try:
        env = viewer_mod.call(sock, "viewer.snapshot", timeout=5.0)
        if "error" in env:
            out.update(reason=env["error"].get("code"),
                       detail=env["error"].get("message"))
        else:
            out.update(ready=True, reason="ready",
                       current_job=env.get("data", {}).get("current_job"),
                       next_seq=env.get("data", {}).get("next_seq"),
                       jobs=len(env.get("data", {}).get("jobs", [])))
    except OSError as e:
        out.update(reason="supervisor_unreachable", detail=str(e))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ready"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="managed run of the supervisor child")
    r.add_argument("--root", required=True)
    r.add_argument("--logs", default=None, help="default <root>/logs")
    r.add_argument("--log-max-bytes", type=int, default=262144)
    r.add_argument("--log-keep", type=int, default=4,
                   help="total retained files per log stream")
    r.add_argument("--min-up-s", type=float, default=2.0)
    r.add_argument("--max-fast", type=int, default=8)
    r.add_argument("--backoff-base", type=float, default=0.5)
    r.add_argument("--backoff-cap", type=float, default=10.0)
    r.add_argument("--kill-grace", type=float, default=10.0)
    r.add_argument("--fake-script", default=None,
                   help="passed through to supervisor_proc (fake backend)")
    r.add_argument("--child-cmd", default=None,
                   help="TEST HOOK ONLY: replace the child command")
    r.set_defaults(fn=cmd_run)

    v = sub.add_parser("viewer", help="observe-only viewer (exec, no child)")
    v.add_argument("--root", required=True)
    # mirrors hina_opencode_worker.viewer's flags (observe-only surface)
    v.add_argument("--watch", action="store_true")
    v.add_argument("--interval", type=float, default=None)
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_viewer)

    s = sub.add_parser("status", help="one snapshot round trip")
    s.add_argument("--root", required=True)
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
