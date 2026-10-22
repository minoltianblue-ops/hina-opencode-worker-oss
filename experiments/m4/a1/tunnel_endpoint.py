#!/usr/bin/env python3
"""M4-A1 probe: LOCALHOST-ONLY tunnel-local endpoint in front of the
stdio MCP proxy.

This is the stand-in for the Secure MCP Tunnel's local hop (MCP.md §1):
a future tunnel connector will dial 127.0.0.1:<port> and pipe
newline-delimited JSON-RPC to us; we pipe those bytes, unparsed, to a
short-lived stdio MCP proxy child and back. Because nothing here parses
frames, proxy stdio stdout purity (T16) survives the hop untouched.

Hard limits enforced by this file (M4-A1 scope — public exposure and
service registration are separate approvals, not defaults here):
- the bind host must be exactly 127.0.0.1; anything else (0.0.0.0, ::,
  a LAN address) is refused BEFORE bind() with exit 2. There is no
  --public option to remove.
- after binding we re-check ``getsockname()``; a non-loopback address
  means we shut down immediately with exit 3.
- one proxy child per TCP connection (MCP.md §1: the proxy is
  short-lived and stateless). Client disconnect closes the child's
  stdin -> the proxy exits; the supervisor and any current job are
  untouched (T15).
- foreground process only; SIGTERM/SIGINT shut everything down cleanly
  and remove the info file. No launchd, no permanent service.

Evidence-grade logging goes to STDERR only (spawn/close per connection,
bound address). The info file records the real bound socket so a probe
can prove loopback-only binding after the fact.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ALLOWED_BIND_HOSTS = ("127.0.0.1",)   # localhost only; ::1 is a contract
#                                      amendment, not an M4-A1 default.
E_OK, E_BIND_REFUSED, E_NOT_LOOPBACK = 0, 2, 3


class BindRefused(Exception):
    pass


def elog(msg: str) -> None:
    sys.stderr.write("%s endpoint: %s\n"
                     % (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime()), msg))
    sys.stderr.flush()


def validate_and_bind(bind: str, port: int) -> socket.socket:
    """Bind a stream listener, refusing any non-localhost endpoint."""
    if bind not in ALLOWED_BIND_HOSTS:
        raise BindRefused(
            "bind host %r is not localhost-only (allowed: %s); public "
            "exposure needs separate approval (M4-A1)"
            % (bind, ",".join(ALLOWED_BIND_HOSTS)))
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((bind, port))
    except OSError as e:
        srv.close()
        raise BindRefused("bind failed: %s" % e)
    host = srv.getsockname()[0]
    if host not in ALLOWED_BIND_HOSTS:
        srv.close()
        raise BindRefused("kernel bound %r, not loopback" % host)
    srv.listen(8)
    srv.settimeout(0.2)
    return srv


class Endpoint:
    def __init__(self, *, srv, sock_path: str, tools_path: str,
                 python: str, info_file: str | None, proxy_log=None):
        self.srv = srv
        self.sock_path = sock_path
        self.tools_path = tools_path
        self.python = python
        self.info_file = info_file
        self.proxy_log = proxy_log
        self.stop = threading.Event()
        self.children: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    # -------------------------------------------------------------- proxy

    def _spawn_proxy(self) -> subprocess.Popen:
        env = os.environ.copy()
        src = Path(__file__).resolve().parents[3] / "src"
        if src.is_dir():
            env["PYTHONPATH"] = (str(src) + os.pathsep
                                 + env.get("PYTHONPATH", ""))
        stderr = subprocess.DEVNULL
        log_fh = None
        if self.proxy_log:
            # 0600, append — proxy logs are already redacted by the proxy
            # itself (stdout purity: everything else goes to stderr).
            log_fh = os.open(self.proxy_log,
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            stderr = log_fh
        proc = subprocess.Popen(
            [self.python, "-m", "hina_opencode_worker.mcp_proxy",
             "--socket", self.sock_path, "--tools", self.tools_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr, env=env)
        if log_fh is not None:
            os.close(log_fh)
        with self._lock:
            self.children.append(proc)
        return proc

    # ------------------------------------------------------- byte pumping

    @staticmethod
    def _pipe(src, dst) -> None:
        """Copy bytes until EOF, then close the write side. Closing
        proc.stdin on client EOF is what ends the short-lived proxy."""
        try:
            while True:
                data = src.recv(65536) if hasattr(src, "recv") \
                    else src.read1(65536) if hasattr(src, "read1") \
                    else src.read(65536)
                if not data:
                    break
                if hasattr(dst, "sendall"):
                    dst.sendall(data)      # socket peer
                else:
                    dst.write(data)        # pipe/file peer
                    dst.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                dst.close()
            except OSError:
                pass

    def _serve_conn(self, conn, addr) -> None:
        proc = self._spawn_proxy()
        elog("conn from %s:%d -> proxy pid %d" % (addr[0], addr[1],
                                                  proc.pid))
        up = threading.Thread(target=self._pipe,
                              args=(conn, proc.stdin), daemon=True)
        down = threading.Thread(target=self._pipe,
                                args=(proc.stdout, conn), daemon=True)
        up.start()
        down.start()
        # When the client is gone the up-pipe closes proxy stdin (EOF); the
        # proxy exits by contract (MCP.md §1) — that is NOT a job cancel.
        up.join()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            elog("proxy pid %d killed after timeout" % proc.pid)
        with self._lock:
            if proc in self.children:
                self.children.remove(proc)
        down.join(timeout=5)   # let the last response bytes reach the peer
        elog("proxy pid %d exited rc=%s; connection closed"
             % (proc.pid, proc.returncode))
        try:
            conn.close()
        except OSError:
            pass

    # ---------------------------------------------------------- lifecycle

    def write_info(self) -> dict:
        host, port = self.srv.getsockname()
        info = {"kind": "m4a1-tunnel-endpoint", "pid": os.getpid(),
                "bound_host": host, "port": port,
                "loopback_only": host in ALLOWED_BIND_HOSTS,
                "supervisor_socket": self.sock_path,
                "tools_doc": self.tools_path,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime())}
        if self.info_file:
            tmp = self.info_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(info, fh, indent=2)
            os.replace(tmp, self.info_file)
        return info

    def serve_forever(self) -> int:
        info = self.write_info()
        elog("listening on %s:%d (loopback-only), proxy target socket=%s"
             % (info["bound_host"], info["port"], self.sock_path))
        while not self.stop.is_set():
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn, addr),
                             daemon=True).start()
        self.shutdown()
        return E_OK

    def shutdown(self) -> None:
        try:
            self.srv.close()
        except OSError:
            pass
        with self._lock:
            procs = list(self.children)
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self.info_file and os.path.exists(self.info_file):
            try:
                os.unlink(self.info_file)
            except OSError:
                pass
        elog("shutdown complete")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--socket", required=True,
                    help="supervisor UDS path handed to every proxy child")
    ap.add_argument("--tools", required=True,
                    help="path to docs/contracts/mcp-tools.json")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="ONLY 127.0.0.1 is accepted (M4-A1)")
    ap.add_argument("--port", type=int, default=0,
                    help="0 = ephemeral; the real port lands in --info-file")
    ap.add_argument("--info-file", default=None,
                    help="where to write bound endpoint facts (JSON)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--proxy-log", default=None,
                    help="0600 append file for proxy stderr (redacted)")
    ns = ap.parse_args(argv)

    try:
        srv = validate_and_bind(ns.bind, ns.port)
    except BindRefused as e:
        elog("REFUSED: %s" % e)
        return E_BIND_REFUSED

    ep = Endpoint(srv=srv, sock_path=ns.socket, tools_path=ns.tools,
                  python=ns.python, info_file=ns.info_file,
                  proxy_log=ns.proxy_log)

    def _stop(signum, _frame):
        elog("signal %d -> stopping" % signum)
        ep.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        return ep.serve_forever()
    except Exception as e:  # never leave a half-listening socket
        elog("fatal: %s" % e)
        ep.shutdown()
        return E_NOT_LOOPBACK


if __name__ == "__main__":
    sys.exit(main())
