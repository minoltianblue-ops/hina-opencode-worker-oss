"""T15 — MCP EOF / tunnel disconnect / viewer close keep the current
task and its results intact, and NEVER spawn follow-on work (MCP.md §1:
transport loss is not a cancel and not a trigger).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

from _p4_support import SRC, TOOLS_JSON, P4TestCase, outcome_completed, rid


def attempts_count(sup, job_id):
    conn = sup._read()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE job_id = ?",
            (job_id,)).fetchone()[0]
    finally:
        conn.close()


class ProxyEoFSurvival(P4TestCase):
    def test_proxy_stdin_eof_leaves_job_running_and_restarts_clean(self):
        self.backend.steps = [("hang", None)] * 1200 + [
            outcome_completed()]
        p = self.proxy()
        p.initialize()
        r = p.call_tool("worker_start", self.start_args(rid()))
        env = r["result"]["structuredContent"]
        self.assertIn("data", env, str(env)[:200])
        jid = env["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        atts0 = attempts_count(self.sup, jid)

        # tunnel EOF: proxy dies, current job untouched (T15)
        p.proc.stdin.close()
        rc = p.proc.wait(timeout=10)
        self.assertEqual(rc, 0, "proxy must exit cleanly on stdin EOF")
        time.sleep(0.6)
        self.assertEqual(self.job_state(jid), "running",
                         "MCP EOF must never cancel the running task")
        self.assertEqual(attempts_count(self.sup, jid), atts0,
                         "EOF must not trigger follow-on work")

        # supervisor still serves a fresh proxy afterwards
        p2 = self.proxy()
        p2.initialize()
        st = p2.call_tool("worker_status", {"job_id": jid})
        self.assertEqual(
            st["result"]["structuredContent"]["data"]["jobs"][0]["state"],
            "running")
        self.finish(jid)

    def test_client_disconnect_mid_request_is_not_a_cancel(self):
        self.backend.steps = [("hang", None)] * 1200 + [
            outcome_completed()]
        a = self.start_args(rid())
        # send over a raw connection and slam it shut before the reply
        import socket
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(self.sock_path)
        payload = json.dumps({"client": "mcp_proxy", "tool": "worker_start",
                              "arguments": a}).encode()
        import struct
        conn.sendall(struct.pack(">I", len(payload)) + payload)
        conn.close()          # abrupt transport loss
        time.sleep(0.4)
        st = self.call("worker_status", {})
        self.assertEqual(len(st["data"]["jobs"]), 1,
                         "disconnect must not drop or duplicate the job")
        jid = st["data"]["jobs"][0]["job_id"]
        # exact same payload on a fresh connection replays, never restarts
        replay = self.call("worker_start", a)
        self.assertTrue(replay["receipt"]["replayed"])
        self.assertEqual(replay["data"]["job"]["job_id"], jid)
        self.wait_for(jid, ("running",))
        self.assertEqual(attempts_count(self.sup, jid), 1)
        self.finish(jid)


class ViewerCloseSurvival(P4TestCase):
    def _viewer_proc(self, *args):
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{SRC}{os.pathsep}" \
                            f"{env.get('PYTHONPATH', '')}"
        return subprocess.Popen(
            [sys.executable, "-m", "hina_opencode_worker.viewer",
             "--socket", self.sock_path, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def test_once_and_watch_exit_leave_job_running(self):
        self.backend.steps = [("hang", None)] * 1200 + [
            outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        out = subprocess.run(
            [sys.executable, "-m", "hina_opencode_worker.viewer",
             "--socket", self.sock_path, "--once", "--json"],
            capture_output=True, timeout=15,
            env={**os.environ,
                 "PYTHONPATH": f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"})
        self.assertEqual(out.returncode, 0)
        snap = json.loads(out.stdout.decode())["data"]
        self.assertEqual(snap["current_job"], jid)
        # watcher: run briefly, then kill the terminal (close = detach)
        v = self._viewer_proc("--watch", "--interval", "0.05")
        time.sleep(0.5)
        v.terminate()
        v.wait(timeout=10)
        time.sleep(0.4)
        self.assertEqual(self.job_state(jid), "running",
                         "viewer close must not touch the job")
        self.finish(jid)

    def test_viewer_output_never_leaks_challenge(self):
        saved = self.config.approval_channel_verified
        self.config.approval_channel_verified = True
        try:
            self.backend.steps = [
                ("permission", {"id": "perm_15",
                                "sessionID": "ses_fake0001",
                                "action": "shell",
                                "resources": ["ls"], "save": []}),
                outcome_completed()]
            jid = self.call("worker_start",
                            self.start_args(rid()))["data"]["job"]["job_id"]
            self.wait_for(jid, ("waiting_approval",))
            st = self.call("worker_status", {"job_id": jid})
            challenge = st["data"]["jobs"][0]["pending_approvals"][0][
                "challenge"]
            self.assertIsNotNone(challenge)
            out = subprocess.run(
                [sys.executable, "-m", "hina_opencode_worker.viewer",
                 "--socket", self.sock_path, "--once", "--json"],
                capture_output=True, timeout=15,
                env={**os.environ,
                     "PYTHONPATH": f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"})
            blob = out.stdout.decode()
            self.assertNotIn(challenge, blob,
                             "§7: challenges never reach the terminal")
            self.assertIn(jid, blob)
            self.finish(jid)
        finally:
            self.config.approval_channel_verified = saved

    def test_viewer_channel_is_read_only(self):
        # viewer client cannot mutate (server-side FORBIDDEN) — a
        # compromised observer still cannot steer the job
        env = self.raw({"client": "viewer", "method": "worker_cancel",
                        "arguments": {"request_id": rid()}})
        self.assertEqual(env["error"]["code"], "FORBIDDEN")
        snap = self.raw({"client": "viewer", "method": "viewer.snapshot",
                         "arguments": {}})
        self.assertEqual(snap["data"]["jobs"], [])
        tail = self.raw({"client": "viewer", "method": "viewer.tail",
                         "arguments": {"after_seq": 0}})
        self.assertIn("events", tail["data"])


if __name__ == "__main__":
    unittest.main()
