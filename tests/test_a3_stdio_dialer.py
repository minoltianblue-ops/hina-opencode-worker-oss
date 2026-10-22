#!/usr/bin/env python3
"""T-A3-2 stdio dialer — byte-pure loopback piping (offline: it pipes
to a test-local echo server on 127.0.0.1; stdout stays frame-pure)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIALER = ROOT / "experiments" / "m4" / "a3" / "stdio_dialer.py"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EchoServer(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(4)
        self.port = port

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    @staticmethod
    def _serve(conn):
        buf = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    conn.sendall(b'{"echo": ' + line + b"}\n")
        except OSError:
            pass
        finally:
            conn.close()


class DialerPiping(unittest.TestCase):
    def _env(self):
        env = dict(os.environ)
        env.pop("M4_A3_LIVE", None)
        return env

    def test_roundtrip_lines_pure_stdout(self):
        port = free_port()
        EchoServer(port).start()
        payload = (json.dumps({"jsonrpc": "2.0", "id": 1,
                               "method": "ping"}) + "\n"
                   + json.dumps({"jsonrpc": "2.0", "id": 2}) + "\n")
        p = subprocess.run([sys.executable, str(DIALER), "--port",
                            str(port)], input=payload.encode(),
                           capture_output=True, timeout=20,
                           env=self._env())
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        lines = p.stdout.decode().splitlines()
        self.assertEqual(len(lines), 2)
        ids = [json.loads(l)["echo"] for l in lines]   # parseable = pure
        self.assertEqual(ids[0]["id"], 1)
        # diagnostics stayed on stderr (stdout frame purity)
        self.assertIn(b"dialer:", p.stderr)

    def test_connect_failure_exit_1(self):
        port = free_port()   # nobody listens
        p = subprocess.run([sys.executable, str(DIALER), "--port",
                            str(port), "--connect-timeout", "1"],
                           input=b"", capture_output=True, timeout=20,
                           env=self._env())
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"cannot reach", p.stderr)

    def test_no_host_option(self):
        # the peer host is a literal — this must not be a general relay
        p = subprocess.run([sys.executable, str(DIALER),
                            "--host", "0.0.0.0", "--port", "1"],
                           input=b"", capture_output=True, timeout=20,
                           env=self._env())
        self.assertEqual(p.returncode, 2)
        self.assertIn(b"unrecognized arguments", p.stderr.lower())


if __name__ == "__main__":
    unittest.main()
