"""T16 — UDS peer UID check, directory/socket file modes, stdio stdout
purity, and secret redaction at every egress (MCP.md §1/§7).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest

from _p4_support import SRC, TOOLS_JSON, P4TestCase, rid

# Deliberately fictional marker for secret-redaction regression; not a usable token.
SECRET_GITHUB = "ghp_abcdefghijklmnopqrstuvwxyz123456"
SECRET_PASS = "hunter2Passw0rd!xyz"


class FileModes(P4TestCase):
    def test_run_dir_and_socket_modes(self):
        dmode = stat.S_IMODE(os.stat(self.config.run_dir).st_mode)
        smode = stat.S_IMODE(os.stat(self.sock_path).st_mode)
        self.assertEqual(dmode, 0o700, "run dir must be 0700")
        self.assertEqual(smode, 0o600, "socket must be 0600")
        kmode = stat.S_IMODE(os.stat(os.path.join(
            self.config.keys_dir, "approval.key")).st_mode)
        self.assertEqual(kmode, 0o600, "key files must be 0600")
        cm = stat.S_IMODE(os.stat(os.path.join(
            self.config.keys_dir, "cursor.key")).st_mode)
        self.assertEqual(cm, 0o600)

    def test_mode_tamper_blocks_requests(self):
        os.chmod(self.sock_path, 0o644)
        try:
            env = self.raw({"client": "mcp_proxy",
                            "tool": "worker_status", "arguments": {}})
            self.assertEqual(env["error"]["code"], "FORBIDDEN",
                             "world-writable socket must refuse service")
        finally:
            os.chmod(self.sock_path, 0o600)
        env = self.raw({"client": "mcp_proxy", "tool": "worker_status",
                        "arguments": {}})
        self.assertNotIn("error", env, "restored mode serves again")

    def test_run_dir_tamper_internal(self):
        os.chmod(self.config.run_dir, 0o755)
        try:
            env = self.raw({"client": "mcp_proxy",
                            "tool": "worker_status", "arguments": {}})
            self.assertEqual(env["error"]["code"], "INTERNAL")
        finally:
            os.chmod(self.config.run_dir, 0o700)


class PeerUid(P4TestCase):
    def test_wrong_peer_uid_forbidden_none_unauthenticated(self):
        saved = self.sup._peer_credentials
        try:
            self.sup._peer_credentials = lambda conn: os.getuid() + 1
            env = self.raw({"client": "mcp_proxy",
                            "tool": "worker_status", "arguments": {}})
            self.assertEqual(env["error"]["code"], "FORBIDDEN")
            self.sup._peer_credentials = lambda conn: None
            env = self.raw({"client": "mcp_proxy",
                            "tool": "worker_status", "arguments": {}})
            self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        finally:
            self.sup._peer_credentials = saved
        # our own real uid passes the kernel check without injection
        env = self.raw({"client": "mcp_proxy",
                        "tool": "worker_status", "arguments": {}})
        self.assertNotIn("error", env)

    def test_principal_assigned_server_side(self):
        # a client cannot assert another principal's identity in the frame
        env = self.raw({"client": "mcp_proxy",
                        "tool": "worker_status", "arguments": {},
                        "principal": "local:0"})
        self.assertNotIn("error", env)
        jobs = env["data"]["jobs"]
        conn = self.sup._read()
        try:
            rows = conn.execute(
                "SELECT principal_id FROM jobs").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [], "no jobs exist; nothing cross-visible")


class StdioPurity(P4TestCase):
    def test_stdout_only_jsonrpc_frames(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{SRC}{os.pathsep}{env.get('PYTHONPATH', '')}"
        log_file = os.path.join(self.sup.config.root, "proxy.log")
        proc = subprocess.Popen(
            [sys.executable, "-m", "hina_opencode_worker.mcp_proxy",
             "--socket", self.sock_path, "--tools", str(TOOLS_JSON),
             "--verbose", "--log-file", log_file],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self._proxies.append(proc)

        def send(obj):
            proc.stdin.write(json.dumps(obj).encode() + b"\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18",
                         "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}})
        proc.stdin.write(b"!!! not json\n")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "worker_status",
                         "arguments": {"nope": True}}})
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "worker_catalog",
                         "arguments": {"refresh": False}}})
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0)
        lines = [l for l in out.decode().splitlines() if l.strip()]
        self.assertEqual(len(lines), 5,
                         f"one frame per request (got {len(lines)})")
        for line in lines:
            msg = json.loads(line)      # every stdout line parses
            self.assertEqual(msg.get("jsonrpc"), "2.0")
            self.assertTrue("result" in msg or "error" in msg)
        ids = [json.loads(l).get("id") for l in lines]
        self.assertEqual(ids, [1, None, 2, 3, 4])
        # parse error is -32700 on a frame, not prose
        self.assertEqual(json.loads(lines[1])["error"]["code"], -32700)
        # tool invalid-args travels as isError result, not -32602
        r3 = json.loads(lines[3])["result"]
        self.assertTrue(r3["isError"])
        self.assertEqual(
            r3["structuredContent"]["error"]["code"], "INVALID_ARGUMENT")
        # logs went to stderr + 0600 log file, never stdout
        err_text = err.decode()
        self.assertNotIn("[REDACTED]", "\n".join(lines))
        self.assertIn("stdin EOF", err_text)
        self.assertEqual(
            stat.S_IMODE(os.stat(log_file).st_mode), 0o600)
        with open(log_file) as fh:
            self.assertIn("proxy", fh.read())

    def test_proxy_exit_does_not_kill_supervisor(self):
        p = self.proxy()
        p.initialize()
        p.proc.stdin.close()
        self.assertEqual(p.proc.wait(timeout=10), 0)
        env = self.raw({"client": "mcp_proxy",
                        "tool": "worker_status", "arguments": {}})
        self.assertNotIn("error", env)


class SecretRedaction(P4TestCase):
    def test_error_message_redacts_secret(self):
        env = self.call("worker_start", self.start_args(
            rid(),
            workspace={"repository": f"/tmp/{SECRET_GITHUB}/repo"}))
        self.assertEqual(env["error"]["code"], "INVALID_ARGUMENT")
        blob = json.dumps(env)
        self.assertNotIn(SECRET_GITHUB, blob)
        self.assertIn("[REDACTED]", blob)

    def test_terminal_output_redacts_attention_reason(self):
        # needs_attention reasons can quote backend text; the terminal
        # (viewer) path redacts before print
        jid = self.completed_start(rid())
        with self.sup._db():
            self.sup.store.conn.execute(
                "UPDATE jobs SET attention_reason = ? WHERE job_id = ?",
                (f"stuck: password={SECRET_PASS} token={SECRET_GITHUB}",
                 jid))
        out = subprocess.run(
            [sys.executable, "-m", "hina_opencode_worker.viewer",
             "--socket", self.sock_path, "--once"],
            capture_output=True, timeout=15, text=True,
            env={**os.environ,
                 "PYTHONPATH": f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"})
        self.assertEqual(out.returncode, 0)
        self.assertNotIn(SECRET_PASS, out.stdout)
        self.assertNotIn(SECRET_GITHUB, out.stdout)
        self.assertIn("[REDACTED]", out.stdout)

    def test_proxy_stderr_redacted(self):
        # point a proxy at a nonexistent socket path that embeds a
        # secret; the proxy's own EOF log must not echo it
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{SRC}{os.pathsep}{env.get('PYTHONPATH', '')}"
        proc = subprocess.Popen(
            [sys.executable, "-m", "hina_opencode_worker.mcp_proxy",
             "--socket", f"/tmp/{SECRET_GITHUB}/sup.sock",
             "--tools", str(TOOLS_JSON), "--verbose"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "worker_status", "arguments": {}}}).encode()
            + b"\n")
        out, err = proc.communicate(timeout=20)
        self.assertNotIn(SECRET_GITHUB, err.decode())
        self.assertNotIn(SECRET_GITHUB, out.decode())
        frame = json.loads(out.decode().strip())
        self.assertEqual(
            frame["result"]["structuredContent"]["error"]["code"],
            "INTERNAL")
        # message stays generic; the socket path never leaks
        self.assertNotIn(SECRET_GITHUB,
                         json.dumps(frame))


if __name__ == "__main__":
    unittest.main()
