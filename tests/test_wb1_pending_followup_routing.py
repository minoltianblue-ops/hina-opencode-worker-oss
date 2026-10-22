"""WB-1..WB-3 — M2-Q2B mcp_proxy followup routing (coordinator-frozen
option A): the worker_followup delivery field (steer/queue, ONLY by
explicit user order — no auto-queue) is legalized in the contract and
routed proxy→supervisor verbatim, and an accepted-but-unfinished
injection is observable through a READ-ONLY pending-request API
(viewer client only; the 8-tool MCP surface is unchanged).

WB-1 contract surface: worker_followup inputSchema exposes
       delivery ∈ [steer, queue] (byte-for-byte the runtime
       DELIVERY_ENUM), admissible only with mode=continue,
       additionalProperties stays false, the 8-tool surface is
       untouched, the proxy serves the updated schema verbatim, M1
       examples still validate, and FILE_MANIFEST pins the exact bytes.
WB-2 routing: the stdio proxy forwards delivery untouched to the
       supervisor (steer reaches the live session exactly once; a
       backend refusal travels back as the same isError envelope) —
       the proxy never queues, never retries, never mutates.
WB-3 pending-request API: viewer.requests lists journal rows still in
       a non-terminal phase (the BACKEND_TIMEOUT accepted window) as
       metadata only — no message bodies — viewer stays read-only, and
       the mcp_proxy client cannot reach the method at all.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from _p4_support import (P4TestCase, rid, tools_doc, validate_against)
from hina_opencode_worker import supervisor as sup_mod
from hina_opencode_worker.mcp_proxy import TOOL_NAMES, Proxy
from hina_opencode_worker.opencode_v2 import DELIVERY_ENUM
from test_uq1_queued_followup import QueuedFollowupBase

ROOT = Path(__file__).resolve().parent.parent
TOOLS_PATH = ROOT / "docs" / "contracts" / "mcp-tools.json"
MANIFEST_PATH = ROOT / "docs" / "contracts" / "FILE_MANIFEST.json"
EXAMPLES_PATH = ROOT / "docs" / "contracts" / "examples.json"


def _followup_schema() -> dict:
    fw = [t for t in tools_doc()["tools"]
          if t["name"] == "worker_followup"][0]
    return fw["inputSchema"]


class WB1ContractSurface(unittest.TestCase):
    def test_wb1_contract_delivery_surface(self):
        s = _followup_schema()
        self.assertEqual(s["properties"]["delivery"],
                         {"type": "string", "enum": ["steer", "queue"]})
        # contract enum stays the OBSERVED enum the runtime enforces
        self.assertEqual(s["properties"]["delivery"]["enum"],
                         list(DELIVERY_ENUM))
        self.assertIs(s["additionalProperties"], False)
        # delivery rides mode=continue only (schema mirror of the
        # supervisor validate rule)
        self.assertIn({"if": {"required": ["delivery"]},
                       "then": {"properties": {"mode":
                                               {"const": "continue"}}}},
                      s["allOf"])
        # 8-tool freeze: names and count unchanged
        names = sorted(t["name"] for t in tools_doc()["tools"])
        self.assertEqual(names, sorted(TOOL_NAMES))
        self.assertEqual(len(names), 8)

    def test_wb1_proxy_serves_updated_schema_verbatim(self):
        # the proxy serves the contract verbatim (T13) — the updated
        # delivery property must be what tools/list returns, and M1
        # example inputs must still validate against the new schema.
        proxy = Proxy(sock_path="/nonexistent/wb1.sock",
                      tools_doc=tools_doc())
        listed = {t["name"]: t for t in proxy.tools_list()["tools"]}
        self.assertEqual(listed["worker_followup"]["inputSchema"],
                         _followup_schema())
        ex = [e for e in json.loads(
            EXAMPLES_PATH.read_text(encoding="utf-8"))["examples"]
            if e["tool"] == "worker_followup"][0]
        self.assertEqual(validate_against(_followup_schema(), ex["input"]),
                         [], "M1 followup example drifted under new schema")

    def test_wb1_file_manifest_pins_contract_bytes(self):
        raw = TOOLS_PATH.read_bytes()
        entry = json.loads(
            MANIFEST_PATH.read_text(encoding="utf-8"))["files"][
            "docs/contracts/mcp-tools.json"]
        self.assertEqual(entry["bytes"], len(raw))
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())


class WB2ProxyRouting(QueuedFollowupBase):
    """The proxy is a pure router for delivery — steer/queue arrive at
    the supervisor untouched, refusals come back untouched."""

    def test_wb2_steer_and_errors_route_verbatim(self):
        jid, att, sid = self._running_job()
        p = self.proxy()
        p.rpc("initialize", {"protocolVersion": "2025-06-18",
                             "capabilities": {},
                             "clientInfo": {"name": "wb2", "version": "0"}})
        listed = {t["name"]: t
                  for t in p.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(listed["worker_followup"]["inputSchema"],
                         _followup_schema())
        # explicit steer over the proxy: exactly one live injection
        rid_ = rid()
        resp = p.call_tool("worker_followup", {
            "request_id": rid_, "user_request_ref": "chat/1", "job_id": jid,
            "expected_attempt_id": att, "mode": "continue",
            "message": "prefer stdlib only", "delivery": "steer"})
        env = resp["result"]["structuredContent"]
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(env["data"]["job"]["job_id"], jid)
        self.assertFalse(env["receipt"]["replayed"])
        self.assertEqual(self.backend.prompts,
                         [(sid, "prefer stdlib only", "steer")])
        # backend refusal: same envelope, isError, no silent accept,
        # no proxy-side retry or queue
        self.backend.prompt_ok = False
        resp2 = p.call_tool("worker_followup", {
            "request_id": rid(), "user_request_ref": "chat/1", "job_id": jid,
            "expected_attempt_id": att, "mode": "continue",
            "message": "second order", "delivery": "queue"})
        env2 = resp2["result"]["structuredContent"]
        self.assertTrue(resp2["result"]["isError"])
        self.assertEqual(env2["error"]["code"], "BACKEND_UNAVAILABLE")
        # one backend call per accepted request, in order — the proxy
        # never re-sends and the refusal was not absorbed
        self.assertEqual(self.backend.prompts,
                         [(sid, "prefer stdlib only", "steer"),
                          (sid, "second order", "queue")])
        # no delivery while busy: M1 JOB_BUSY verbatim — the proxy does
        # NOT auto-queue anything
        resp3 = p.call_tool("worker_followup", {
            "request_id": rid(), "user_request_ref": "chat/1", "job_id": jid,
            "expected_attempt_id": att, "mode": "continue",
            "message": "no delivery"})
        env3 = resp3["result"]["structuredContent"]
        self.assertEqual(env3["error"]["code"], "JOB_BUSY")
        self.assertEqual(self.job_state(jid), "running")


class WB3ViewerPendingRequests(QueuedFollowupBase):
    def _viewer(self, method, arguments=None):
        return self.raw({"client": "viewer", "method": method,
                         "arguments": arguments or {}})

    def test_wb3_accepted_injection_visible_read_only(self):
        jid, att, _ = self._running_job()
        # an accepted steer that COMPLETED must not be pending
        rid_done = rid()
        env = self._followup(jid, att, rid_done, delivery="steer")
        self.assertIn("data", env)
        # an injection stuck in the accepted window (BACKEND_TIMEOUT,
        # §4 — never extended, never re-sent) IS pending
        self.backend.prompt_sleep = sup_mod.CONTROL_TIMEOUT_S + 1.5
        rid_stuck = rid()
        env = self._followup(jid, att, rid_stuck, delivery="steer")
        self.assertEqual(env["error"]["code"], "BACKEND_TIMEOUT")

        resp = self._viewer("viewer.requests")
        self.assertEqual(resp["contract_version"], "0.3")
        self.assertEqual(resp["method"], "viewer.requests")
        rows = resp["data"]["requests"]
        stuck = [r for r in rows if r["request_id"] == rid_stuck]
        self.assertEqual(len(stuck), 1, str(rows))
        self.assertEqual(
            set(stuck[0]), {"request_id", "tool", "phase", "payload_sha256",
                            "created_at", "updated_at"})
        self.assertEqual(stuck[0]["tool"], "worker_followup")
        self.assertEqual(stuck[0]["phase"], "accepted")
        # metadata only — message bodies never travel to the viewer
        self.assertNotIn("prefer stdlib only", json.dumps(resp))
        done = [r for r in rows if r["request_id"] == rid_done]
        self.assertEqual(done, [], "completed request still listed pending")
        # read-only: viewer cannot touch any tool, and the pending
        # view is not reachable through the mcp_proxy client
        for probe in ("worker_followup", "viewer.snapshot_x"):
            self.assertEqual(
                self._viewer(probe)["error"]["code"], "FORBIDDEN")
        denied = self.raw({"client": "mcp_proxy", "tool": "viewer.requests",
                           "arguments": {}})
        self.assertEqual(denied["error"]["code"], "INVALID_ARGUMENT")
        # exactly one injection per accepted request (§4: the timed-out
        # one was never re-sent after BACKEND_TIMEOUT)
        self.assertEqual(len(self.backend.prompts), 2)


if __name__ == "__main__":
    unittest.main()
