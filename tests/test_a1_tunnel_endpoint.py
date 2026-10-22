"""T-A1-1: the tunnel endpoint is localhost-ONLY (M4-A1 acceptance).

The endpoint is the stand-in for the Secure MCP Tunnel's local hop
(MCP.md §1). Public exposure is a separate approval, so the ONLY
acceptable failure mode for a non-loopback bind is refusal BEFORE any
socket is created. We also prove the real bound address is loopback
(post-bind getsockname re-check) and that shutdown reaps the
short-lived proxy children.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _p4_support import ROOT, TOOLS_JSON

sys.path.insert(0, str(ROOT / "experiments" / "m4" / "a1"))
import tunnel_endpoint  # noqa: E402

ENDPOINT = ROOT / "experiments" / "m4" / "a1" / "tunnel_endpoint.py"


def spawn_endpoint(sock_path, info_file):
    ep = subprocess.Popen(
        [sys.executable, str(ENDPOINT), "--socket", sock_path,
         "--tools", str(TOOLS_JSON), "--port", "0",
         "--info-file", str(info_file)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH":
             str(ROOT / "src") + ":" + str(ROOT / "tests")})
    deadline = time.time() + 10
    while not info_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    return ep


class BindPolicyTest(unittest.TestCase):
    def test_non_loopback_bind_refused_before_bind(self):
        for bad in ("0.0.0.0", "9.9.9.9", "localhost", "::1", ""):
            with self.subTest(bind=bad):
                with self.assertRaises(tunnel_endpoint.BindRefused):
                    tunnel_endpoint.validate_and_bind(bad, 0)

    def test_main_returns_exit_2_on_refused_bind(self):
        rc = tunnel_endpoint.main(["--socket", "/nonexistent/x.sock",
                                   "--tools", str(TOOLS_JSON),
                                   "--bind", "0.0.0.0", "--port", "0"])
        self.assertEqual(rc, tunnel_endpoint.E_BIND_REFUSED)

    def test_127_bind_binds_loopback(self):
        srv = tunnel_endpoint.validate_and_bind("127.0.0.1", 0)
        try:
            host, port = srv.getsockname()
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
        finally:
            srv.close()


class EndpointLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a1ep_"))
        self.info = self.tmp / "endpoint-info.json"
        self.ep = spawn_endpoint(str(self.tmp / "no-such.sock"), self.info)
        self.addCleanup(self._teardown)

    def _teardown(self):
        if self.ep.poll() is None:
            self.ep.kill()
            self.ep.wait(timeout=5)

    def _info(self):
        return json.loads(self.info.read_text(encoding="utf-8"))

    def test_info_records_loopback_bound_address(self):
        info = self._info()
        self.assertEqual(info["bound_host"], "127.0.0.1")
        self.assertTrue(info["loopback_only"])
        self.assertGreater(info["port"], 0)

    def test_client_disconnect_leaves_no_proxy_child(self):
        info = self._info()
        conn = socket.create_connection(("127.0.0.1", info["port"]),
                                        timeout=5)
        conn.sendall(b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
                     b'"params":{"protocolVersion":"2025-06-18",'
                     b'"capabilities":{},"clientInfo":{"name":"t",'
                     b'"version":"0"}}}\n')
        reply = conn.recv(65536)
        self.assertIn(b'"result"', reply,
                      "initialize reply must travel back through the "
                      "endpoint down-pipe — transport is two-way")
        conn.close()
        deadline = time.time() + 10
        children = ["?"]
        while time.time() < deadline:
            ps = subprocess.run(["ps", "-o", "pid=", "--ppid",
                                 str(info["pid"])],
                                capture_output=True, text=True)
            children = ps.stdout.split()
            if not children:
                break
            time.sleep(0.1)
        self.assertEqual(children, [])

    def test_sigterm_is_clean_and_removes_info_file(self):
        info = self._info()
        self.ep.terminate()
        self.assertEqual(self.ep.wait(timeout=10), 0)
        self.assertFalse(self.info.exists())
        ps = subprocess.run(["ps", "-o", "pid=", "--ppid", str(info["pid"])],
                            capture_output=True, text=True)
        self.assertEqual(ps.stdout.split(), [])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", info["port"]),
                                     timeout=2)


if __name__ == "__main__":
    unittest.main()
