#!/usr/bin/env python3
"""Manually started A3 tunnel supervision; never dispatches/replays a worker job.

The operator supplies a hash-pinned manifest. Credentials are obtained from an
existing Keychain helper into the child environment only. This does not install
launchd or restart the supervisor, endpoint, OpenCode, or cokacremote.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time


class Refused(RuntimeError):
    """A bounded, credential-free error message suitable for an operator log."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_spec(spec: dict) -> None:
    required = ('root', 'binary', 'profile', 'keychain_helper', 'keychain_service', 'pins')
    if any(key not in spec for key in required):
        raise Refused('manifest fields missing')
    for key in ('root', 'binary', 'profile', 'keychain_helper'):
        if not isinstance(spec[key], str) or not Path(spec[key]).is_absolute():
            raise Refused('manifest paths must be absolute')
    if not isinstance(spec['keychain_service'], str) or not spec['keychain_service']:
        raise Refused('Keychain service missing')
    pins = spec['pins']
    if not isinstance(pins, dict) or not all(spec[k] in pins for k in ('binary', 'profile', 'keychain_helper')):
        raise Refused('required file hash missing')
    for filename, expected in pins.items():
        try:
            actual = sha256(Path(filename))
        except OSError:
            raise Refused('pinned file unavailable') from None
        if actual != expected:
            raise Refused('pinned file changed; explicit operator review required')


def clean_environment(source: dict[str, str]) -> dict[str, str]:
    # Allowlist rather than a blacklist: profile env overrides must not bleed
    # over from another tunnel, including credentials, MCP, health and proxy vars.
    allowed = ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR')
    return {name: source[name] for name in allowed if name in source}


def child_environment(spec: dict) -> dict[str, str]:
    env = clean_environment(dict(os.environ))
    try:
        result = subprocess.run(
            [spec['keychain_helper'], 'get', spec['keychain_service']],
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('Keychain helper unavailable') from None
    key = result.stdout.strip()
    if result.returncode != 0 or not key.startswith('sk-') or len(key) < 40 or any(c.isspace() for c in key):
        raise Refused('Keychain credential unavailable or invalid')
    env['CONTROL_PLANE_API_KEY'] = key
    return env


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def write_state(root: Path, state: str, **fields) -> None:
    document = {'observed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'guard_pid': os.getpid(), 'state': state, **fields}
    fd, tmp = tempfile.mkstemp(prefix='.a3-guard-', dir=root / 'run')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(document, out, sort_keys=True)
            out.write('\n')
        os.replace(tmp, root / 'run/a3-tunnel-guard.json')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(json.dumps(document, sort_keys=True), file=sys.stderr, flush=True)


def private_append(path: Path) -> int:
    """Open only an owned, singly-linked regular log; tighten existing modes.

    O_NOFOLLOW prevents final-component symlink substitution, O_NONBLOCK
    avoids hanging on a FIFO before fstat can reject it. Parent directories
    belong to the operator's private runtime root.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                 os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise Refused('tunnel log must be an owned singly-linked regular file')
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def stop_child(child: subprocess.Popen) -> None:
    # Child is always our own new session; never signal a PID read from disk.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def supervise(spec: dict, stop: threading.Event, *, preflight=True,
              initial_delay=5.0, max_restarts=5, stable_seconds=300.0) -> int:
    """Bounded process recovery, NOT application-request retry.

    The optional parameters make deterministic offline tests possible; the CLI
    always uses conservative production defaults and loopback preflight.
    """
    root = Path(spec['root'])
    for name in ('run', 'logs'):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(root / 'run/a3-tunnel-guard.lock', os.O_CREAT | os.O_RDWR, 0o600)
    child = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('another A3 tunnel guard holds the lock') from None
        failures = 0
        starts = 0
        while not stop.is_set():
            verify_spec(spec)
            if preflight and port_open(8081):
                raise Refused('A3 health port already owned; refusing duplicate tunnel')
            if preflight and not port_open(8899):
                raise Refused('local A3 endpoint unavailable; worker stack not changed')
            env = child_environment(spec)
            log_fd = private_append(root / 'logs/a3-tunnel-client.log')
            try:
                child = subprocess.Popen(
                    [spec['binary'], 'run', '--profile-file', spec['profile']],
                    stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                    env=env, start_new_session=True)
            finally:
                os.close(log_fd)
                env.pop('CONTROL_PLANE_API_KEY', None)
            starts += 1
            started = time.monotonic()
            write_state(root, 'RUNNING', child_pid=child.pid, starts=starts,
                        readiness='NOT_CHECKED')
            while child.poll() is None and not stop.wait(0.2):
                pass
            if stop.is_set():
                stop_child(child)
                child = None
                write_state(root, 'STOPPED', starts=starts, reason='operator_stop')
                return 0
            code = child.returncode
            uptime = time.monotonic() - started
            stop_child(child)  # also terminate any leftover owned bridge child
            child = None
            if uptime >= stable_seconds:
                failures = 0
            failures += 1
            if failures > max_restarts:
                write_state(root, 'GAVE_UP', starts=starts, exit_code=code,
                            reason='bounded_restart_budget_exhausted')
                return 70
            delay = min(60.0, initial_delay * 2 ** (failures - 1))
            write_state(root, 'BACKOFF', starts=starts, exit_code=code,
                        retry_delay_s=delay, job_replay=False)
            stop.wait(delay)
        write_state(root, 'STOPPED', starts=starts, reason='operator_stop')
        return 0
    finally:
        if child is not None:
            stop_child(child)
        os.close(lock_fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--spec', type=Path, required=True)
    ns = parser.parse_args(argv)
    stop = threading.Event()
    def on_signal(_signum, _frame):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    os.umask(0o077)
    try:
        spec = json.loads(ns.spec.read_text())
        verify_spec(spec)
        return supervise(spec, stop)
    except (Refused, OSError, ValueError, TypeError) as error:
        # Only Refused messages are designed for logs. Never dump helper output.
        message = str(error) if isinstance(error, Refused) else type(error).__name__
        print('A3 guard refused: ' + message, file=sys.stderr)
        return 78


if __name__ == '__main__':
    sys.exit(main())
