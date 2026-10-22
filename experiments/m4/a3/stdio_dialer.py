#!/usr/bin/env python3
"""Byte-pure stdio <-> fixed loopback TCP bridge for the A3 tunnel.

No frame parsing, reconnect, replay, or job cancellation occurs here. A lost
peer must close this process even when upstream stdin remains open; otherwise
tunnel-client can keep advertising a dead transport. Client EOF half-closes
TCP and drains the final response. Only transport draining has a deadline.
"""
from __future__ import annotations

import argparse
import math
import os
import select
import socket
import sys
import time

HOST = '127.0.0.1'  # Deliberately not configurable: never a general relay.
CHUNK = 65536
BUFFER_LIMIT = 4 * CHUNK
DRAIN_SECONDS = 5.0


def elog(message: str) -> None:
    sys.stderr.write('%s dialer: %s\n' % (
        time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), message))
    sys.stderr.flush()


def relay(conn: socket.socket, stdin_fd: int, stdout_fd: int) -> int:
    """Forward available bytes with bounded buffers and symmetric EOF handling.

    Raw nonblocking I/O avoids BufferedReader.read(n)'s fill-or-EOF wait and
    avoids daemon threads blocked on stdin at interpreter shutdown. A TCP
    failure is never retried: forwarded requests may already have executed.
    """
    blocking = {fd: os.get_blocking(fd) for fd in (stdin_fd, stdout_fd)}
    to_peer = bytearray()
    to_stdout = bytearray()
    stdin_open = True
    peer_open = True
    write_closed = False
    premature_eof = False
    drain_deadline = None

    def lose_peer(error: OSError) -> None:
        nonlocal peer_open, premature_eof, drain_deadline
        # A failed TCP transport must not discard bytes already received.
        # Stop upstream writes, drain only our bounded output buffer, then
        # report failure. Never reconnect or replay an ambiguous request.
        peer_open = False
        premature_eof = True
        to_peer.clear()
        drain_deadline = time.monotonic() + DRAIN_SECONDS
        elog('peer failure: %s; draining received bytes only' % type(error).__name__)

    try:
        for fd in blocking:
            os.set_blocking(fd, False)
        conn.setblocking(False)
        while True:
            if not stdin_open and not to_peer and not write_closed and peer_open:
                try:
                    conn.shutdown(socket.SHUT_WR)
                    write_closed = True
                    drain_deadline = time.monotonic() + DRAIN_SECONDS
                except OSError as error:
                    lose_peer(error)
            if not peer_open and not to_stdout:
                if premature_eof:
                    elog('peer EOF while upstream open; no reconnect or replay')
                return 1 if premature_eof else 0
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                elog('transport drain deadline reached; job unaffected')
                return 1
            readers = []
            if stdin_open and peer_open and len(to_peer) < BUFFER_LIMIT:
                readers.append(stdin_fd)
            if peer_open and len(to_stdout) < BUFFER_LIMIT:
                readers.append(conn)
            writers = []
            if to_peer and peer_open:
                writers.append(conn)
            if to_stdout:
                writers.append(stdout_fd)
            timeout = None if drain_deadline is None else max(
                0.0, drain_deadline - time.monotonic())
            ready_r, ready_w, _ = select.select(readers, writers, [], timeout)
            # Read stdin first when both streams signal EOF in the same cycle.
            if stdin_fd in ready_r:
                try:
                    data = os.read(stdin_fd, min(CHUNK, BUFFER_LIMIT - len(to_peer)))
                    if data:
                        to_peer.extend(data)
                    else:
                        stdin_open = False
                except BlockingIOError:
                    pass
            if conn in ready_r:
                try:
                    data = conn.recv(min(CHUNK, BUFFER_LIMIT - len(to_stdout)))
                    if data:
                        to_stdout.extend(data)
                    else:
                        premature_eof = stdin_open or bool(to_peer)
                        peer_open = False
                        # Do not forward or retry anything after a peer is lost.
                        to_peer.clear()
                        drain_deadline = time.monotonic() + DRAIN_SECONDS
                except BlockingIOError:
                    pass
                except OSError as error:
                    lose_peer(error)
            if conn in ready_w and peer_open and to_peer:
                try:
                    sent = conn.send(to_peer)
                    if sent == 0:
                        raise ConnectionError('zero-byte socket write')
                    del to_peer[:sent]
                except BlockingIOError:
                    pass
                except OSError as error:
                    lose_peer(error)
            if stdout_fd in ready_w and to_stdout:
                try:
                    written = os.write(stdout_fd, to_stdout)
                    if written == 0:
                        raise ConnectionError('zero-byte stdout write')
                    del to_stdout[:written]
                except BlockingIOError:
                    pass
    except (OSError, ValueError) as error:
        # Never include payloads, secrets, or arbitrary exception strings.
        elog('transport failure: %s; no replay' % type(error).__name__)
        return 1
    finally:
        for fd, was_blocking in blocking.items():
            try:
                os.set_blocking(fd, was_blocking)
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--connect-timeout', type=float, default=10.0)
    ns = ap.parse_args(argv)
    if not (0 < ns.port < 65536):
        elog('bad port')
        return 2
    if not math.isfinite(ns.connect_timeout) or ns.connect_timeout <= 0:
        elog('connect-timeout must be finite and positive')
        return 2
    try:
        with socket.create_connection((HOST, ns.port), timeout=ns.connect_timeout) as conn:
            elog('connected to %s:%d (pid %d)' % (HOST, ns.port, os.getpid()))
            return relay(conn, sys.stdin.fileno(), sys.stdout.fileno())
    except OSError as error:
        elog('cannot reach %s:%d: %s' % (HOST, ns.port, type(error).__name__))
        return 1


if __name__ == '__main__':
    sys.exit(main())
