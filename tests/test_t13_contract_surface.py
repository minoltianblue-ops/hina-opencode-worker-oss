"""T13 — schema surface: 8 tools' inputs/outputs per mcp-tools.json,
invalid fields refused, NORMAL wait timeout, and every contract error
code produced by a real trigger path (no monkeypatched returns).
"""
from __future__ import annotations

import json
import os
import time
import unittest

from _p4_support import (
    P4TestCase, fake_att_id, fake_job_id, assert_envelope, outcome_completed, rid, tools_doc,
    validate_against, output_schema)
from hina_opencode_worker import supervisor as sup_mod
from hina_opencode_worker.supervisor import (
    BackendUnavailable, ContractMismatch, ERROR_CODES)

PERM = {"id": "perm_t13", "sessionID": "ses_fake0001", "action": "shell",
        "resources": ["ls -la"], "save": []}


class ToolsListContract(P4TestCase):
    def test_proxy_serves_contract_verbatim(self):
        p = self.proxy()
        p.initialize()
        got = p.rpc("tools/list")["result"]["tools"]
        want = {t["name"]: t for t in tools_doc()["tools"]}
        self.assertEqual(sorted(t["name"] for t in got),
                         sorted(want), "tool set must equal the contract")
        for t in got:
            w = want[t["name"]]
            self.assertEqual(t["inputSchema"], w["inputSchema"],
                             f"{t['name']} inputSchema drift")
            self.assertEqual(t["outputSchema"], w["outputSchema"],
                             f"{t['name']} outputSchema drift")
            self.assertEqual(t["description"], w["description"])
            self.assertEqual(t.get("annotations", {}),
                             w.get("annotations", {}))

    def test_protocol_version_never_echoed_unsupported(self):
        p = self.proxy()
        r = p.rpc("initialize", {"protocolVersion": "1999-01-01",
                                 "capabilities": {},
                                 "clientInfo": {"name": "t",
                                                "version": "0"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        p.initialize()  # valid one
        self.assertEqual(
            p.rpc("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "t",
                                                "version": "0"}})
            ["result"]["protocolVersion"], "2024-11-05")


class OutputSchemasHappy(P4TestCase):
    def test_all_tools_envelopes_match_output_schema(self):
        # catalog
        cat = self.call("worker_catalog", {"refresh": True})
        assert_envelope(self, cat, "worker_catalog")
        # start -> completed
        jid = self.completed_start(rid())
        st = self.call("worker_status", {"job_id": jid})
        assert_envelope(self, st, "worker_status")
        res = self.call("worker_result", {"job_id": jid})
        assert_envelope(self, res, "worker_result")
        # wait on terminal job returns immediately
        w = self.call("worker_wait", {"job_id": jid, "wait_ms": 50})
        assert_envelope(self, w, "worker_wait")
        # cancel a completed job: accepted, but nothing to cancel
        c = self.call("worker_cancel",
                      {"request_id": rid(), "job_id": jid,
                       "expected_attempt_id":
                           st["data"]["jobs"][0]["active_attempt_id"]})
        assert_envelope(self, c, "worker_cancel")
        self.assertFalse(c["data"]["cancel_requested"])
        # followup continue on completed (backend idle) -> new attempt
        f = self.call("worker_followup",
                      {"request_id": rid(),
                       "user_request_ref": "req/2026/0002",
                       "job_id": jid,
                       "expected_attempt_id":
                           st["data"]["jobs"][0]["active_attempt_id"],
                       "mode": "continue",
                       "goal": "one more look"})
        assert_envelope(self, f, "worker_followup")
        self.wait_for(jid, ("completed", "failed"))

    def test_error_envelopes_match_output_schema(self):
        env = self.call("worker_status",
                        {"job_id": fake_job_id()})
        assert_envelope(self, env, "worker_status")
        self.assertEqual(env["error"]["code"], "NOT_FOUND")
        # every outputSchema accepts the error branch shape
        errs = validate_against(output_schema("worker_status"), env)
        self.assertEqual(errs, [], "error envelope violates outputSchema")
        rec = env["receipt"]
        self.assertIsNone(rec["request_id"])
        self.assertFalse(rec["replayed"])


class InvalidInputs(P4TestCase):
    BAD = {
        "unknown property": ("worker_catalog", {"refresh": False,
                                                "extra": 1}),
        "wrong type": ("worker_catalog", {"refresh": "yes"}),
        "missing required": ("worker_start", {"goal": "x"}),
        "non-object args": ("worker_wait", "job_1"),
        "bad request_id": ("worker_cancel",
                           {"request_id": "req_NOT-uuid", "job_id":
                            fake_job_id(), "expected_attempt_id":
                            fake_att_id()}),
        "wait_ms too large": ("worker_wait",
                              {"job_id": fake_job_id(),
                               "wait_ms": 25001}),
        "wait_ms bool": ("worker_wait",
                         {"job_id": fake_job_id(), "wait_ms": True}),
        "status mixing": ("worker_status",
                          {"job_id": fake_job_id(),
                           "states": ["running"]}),
        "bad state enum": ("worker_status", {"states": ["exploded"]}),
        "model pattern": ("worker_start",
                          {"request_id": rid(),
                           "user_request_ref": "r", "goal": "g",
                           "workspace": {"repository": "/tmp"},
                           "profile": "inspect", "model": "no-slash",
                           "constraints": [], "done_when": ["d"]}),
        "inspect+vercmd": ("worker_start",
                           {"request_id": rid(),
                            "user_request_ref": "r", "goal": "g",
                            "workspace": {"repository": "/tmp"},
                            "profile": "inspect", "model": "fake/free",
                            "constraints": [], "done_when": ["d"],
                            "verification_commands":
                                [{"argv": ["true"]}]}),
        "inc without ctx": ("worker_start",
                            {"request_id": rid(),
                             "user_request_ref": "r", "goal": "g",
                             "workspace": {"repository": "/tmp",
                                           "include_existing_changes":
                                               True},
                             "profile": "inspect", "model": "fake/free",
                             "constraints": [], "done_when": ["d"]}),
        "inc+committed": ("worker_start",
                          {"request_id": rid(),
                           "user_request_ref": "r", "goal": "g",
                           "workspace": {"repository": "/tmp",
                                         "include_existing_changes": True,
                                         "context_files": ["a"],
                                         "use_committed_only": True},
                           "profile": "inspect", "model": "fake/free",
                           "constraints": [], "done_when": ["d"]}),
    }

    def test_invalid_fields_all_INVALID_ARGUMENT(self):
        for name, (tool, args) in self.BAD.items():
            with self.subTest(name):
                env = self.call(tool, args)
                self.assertIn("error", env, f"{name} was accepted")
                self.assertEqual(env["error"]["code"],
                                 "INVALID_ARGUMENT", name)
                self.assertTrue(env["error"]["retryable"] is False)

    def test_proxy_protocol_errors(self):
        p = self.proxy()
        p.initialize()
        p.send_bytes(b"{not json\n")
        self.assertEqual(p.recv()["error"]["code"], -32700)
        r = p.rpc("no/such_method")
        self.assertEqual(r["error"]["code"], -32601)
        r = p.rpc("tools/call", {"name": "worker_nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        p.send_raw({"jsonrpc": "1.0", "id": 9, "method": "tools/list"})
        self.assertEqual(p.recv()["error"]["code"], -32600)
        # tool-level invalid input is NOT a protocol error: isError result
        r = p.call_tool("worker_catalog", {"nope": 1})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["error"]["code"],
                         "INVALID_ARGUMENT")
        self.assertNotIn("error", r)


class WaitNormalTimeout(P4TestCase):
    def test_wait_returns_normally_at_budget(self):
        self.backend.steps = [("hang", None)] * 400 + [outcome_completed()]
        jid = None
        env = self.call("worker_start", self.start_args(rid()))
        jid = env["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        t0 = time.monotonic()
        w = self.call("worker_wait", {"job_id": jid, "wait_ms": 400})
        dt = time.monotonic() - t0
        assert_envelope(self, w, "worker_wait")
        self.assertTrue(w["data"]["wait_expired"])
        self.assertNotIn("error", w,
                         "a plain timeout is NOT an error envelope")
        self.assertLess(dt, 3.0, "wait overshot its budget")


class ErrorCodeCoverage(P4TestCase):
    """Every one of the 30 contract codes from a real trigger."""

    def _err(self, tool, args, code, msg=""):
        env = self.call(tool, args)
        self.assertIn("error", env, f"{msg or code}: expected error")
        self.assertEqual(env["error"]["code"], code,
                         f"{msg} got {env.get('error')}")
        self.assertLessEqual(len(env["error"]["message"]), 2000)
        self.assertIsInstance(env["error"]["retryable"], bool)
        self.assertLessEqual(len(env["error"]["details"]), 16)
        return env

    def test_catalog_and_argument_codes(self):
        self._err("worker_status", {"limit": "x"}, "INVALID_ARGUMENT")
        self._err("worker_status", {"job_id": fake_job_id()},
                  "NOT_FOUND")
        self._err("worker_start", self.start_args(rid(),
                                                  model="nope/x"),
                  "MODEL_NOT_ALLOWED")
        self._err("worker_start", self.start_args(rid(), model="fake/off"),
                  "MODEL_UNAVAILABLE")
        self.backend.probe_error = ContractMismatch("framing v9 unknown")
        self._err("worker_catalog", {"refresh": True}, "CONTRACT_MISMATCH")
        self.backend.probe_error = BackendUnavailable("socket dead")
        self._err("worker_catalog", {"refresh": True},
                  "BACKEND_UNAVAILABLE")
        self.backend.probe_error = None
        flag = self.config.develop_enforcement_verified
        self.config.develop_enforcement_verified = False
        try:
            self._err("worker_start",
                      self.start_args(rid(), profile="develop"),
                      "CAPABILITY_UNVERIFIED")
        finally:
            self.config.develop_enforcement_verified = flag

    def test_busy_and_idempotency(self):
        self.backend.steps = [("hang", None)] * 300 + [
            outcome_completed()]
        a1 = self.start_args(rid())
        env = self.call("worker_start", a1)
        self.assertIn("data", env)
        self._err("worker_start", self.start_args(rid()), "BUSY")
        self.finish(env["data"]["job"]["job_id"])
        a1b = dict(a1)
        a1b["goal"] = "a different payload under the same request_id"
        self._err("worker_start", a1b, "IDEMPOTENCY_CONFLICT")
        # replay of the ORIGINAL payload keeps its receipt
        replay = self.call("worker_start", a1)
        self.assertTrue(replay["receipt"]["replayed"])

    def test_workspace_codes(self):
        from _wt_support import git, write
        write(self.repo / "dirty.txt", "untracked\n")
        self._err("worker_start", self.start_args(rid()),
                  "WORKSPACE_DIRTY")
        # include_existing_changes clears the dirty gate
        self.backend.steps = [outcome_completed()]
        env = self.call("worker_start", self.start_args(
            rid(),
            workspace={"repository": str(self.repo),
                       "include_existing_changes": True,
                       "context_files": ["dirty.txt"]}))
        self.assertIn("data", env, str(env))
        self.wait_for(env["data"]["job"]["job_id"],
                      ("completed", "failed"))
        from _wt_support import commit_all
        commit_all(self.repo, "capture dirty input baseline")
        # WORKSPACE_CONFLICT: a live lease on the canonical path blocks
        # a fresh start (lease table is the arbiter, not memory)
        # a live lease on the canonical path (never released) blocks a
        # fresh start — the lease table is the arbiter, not memory
        jid = self.completed_start(rid())
        att = self.sup._job_row(jid)["active_attempt_id"]
        with self.sup._db():
            self.sup.store.acquire_workspace_lease(
                canonical_path=os.path.realpath(str(self.repo)),
                job_id=jid, attempt_id=att,
                executor_id="ghost",
                git_common_dir=os.path.realpath(str(self.repo / ".git")),
                base_revision="0" * 40, owner_pid=os.getpid())
        self._err("worker_start", self.start_args(rid()),
                  "WORKSPACE_CONFLICT")

    def test_cursor_and_result_codes(self):
        env = self.call("worker_status", {"limit": 5})
        self._err("worker_result", {"job_id": fake_job_id()},
                  "NOT_FOUND")
        # RESULT_NOT_READY: running job has no result yet
        self.backend.steps = [("hang", None)] * 300 + [
            outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        self._err("worker_result", {"job_id": jid}, "RESULT_NOT_READY")
        self.finish(jid)

    def test_approval_codes(self):
        # channel unverified (default harness) -> challenge hidden and
        # approval blocked
        self.backend.steps = [("permission", PERM), outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("waiting_approval",))
        st = self.call("worker_status", {"job_id": jid})
        pa = st["data"]["jobs"][0]["pending_approvals"]
        self.assertTrue(pa, "pending approval must surface")
        self.assertIsNone(pa[0]["challenge"],
                          "§7: unverified channel hides the challenge")
        self.assertEqual(pa[0].get("channel_status", "unavailable"),
                         "unavailable")
        self._err("worker_approve",
                  {"request_id": rid(), "job_id": jid,
                   "attempt_id": st["data"]["jobs"][0]
                   ["active_attempt_id"],
                   "approval_id": pa[0]["approval_id"],
                   "challenge": "A" * 43, "decision": "approve",
                   "user_decision_ref": "ui/t13"},
                  "APPROVAL_CHANNEL_UNAVAILABLE")
        # cancel out of waiting_approval to unblock teardown
        self.call("worker_cancel",
                  {"request_id": rid(), "job_id": jid,
                   "expected_attempt_id": st["data"]["jobs"][0]
                   ["active_attempt_id"]})
        self.wait_for(jid, ("cancelled", "needs_attention"))


class T13NeedsAttentionAndTransport(P4TestCase):
    def test_backend_timeout_needs_attention_and_reconcile_codes(self):
        self.backend.start_error = sup_mod.BackendTimeout("start ack lost")
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("needs_attention",))
        self.assertTrue(self.sup._job_row(jid)["attention_reason"]
                        .startswith("BACKEND_TIMEOUT"))
        # needs_attention is ACTIVE: another start is BUSY not a queue
        self._err("worker_start", self.start_args(rid()), "BUSY")

    def _err(self, tool, args, code):
        env = self.call(tool, args)
        self.assertIn("error", env, f"expected {code}")
        self.assertEqual(env["error"]["code"], code, str(env)[:300])
        return env

    def test_unauth_and_forbidden_wire(self):
        env = self.raw({"client": "telegram_bot",
                        "tool": "worker_status", "arguments": {}})
        self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        saved = self.sup._peer_credentials
        self.sup._peer_credentials = lambda conn: 0
        try:
            env = self.raw({"client": "mcp_proxy",
                            "tool": "worker_status", "arguments": {}})
            self.assertEqual(env["error"]["code"], "FORBIDDEN")
        finally:
            self.sup._peer_credentials = saved
        self.sup._peer_credentials = lambda conn: None
        try:
            env = self.raw({"client": "mcp_proxy",
                            "tool": "worker_status", "arguments": {}})
            self.assertEqual(env["error"]["code"], "UNAUTHENTICATED")
        finally:
            self.sup._peer_credentials = saved
        # viewer client cannot mutate
        env = self.raw({"client": "viewer", "method": "worker_start",
                        "arguments": {}})
        self.assertEqual(env["error"]["code"], "FORBIDDEN")

    def test_all_error_codes_are_reachable_names(self):
        self.assertEqual(len(ERROR_CODES), 30)
        self.assertEqual(ERROR_CODES, sup_mod.ERROR_CODES)


class T13ApprovalChannelVerified(P4TestCase):
    approval_channel_verified = True

    def test_approve_happy_invalid_and_expired(self):
        self.backend.steps = [("permission", PERM), outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("waiting_approval",))
        st = self.call("worker_status", {"job_id": jid})
        job = st["data"]["jobs"][0]
        pa = job["pending_approvals"][0]
        self.assertIsNotNone(pa["challenge"],
                             "verified channel issues the challenge")
        # wrong challenge -> APPROVAL_INVALID
        bad = self.call("worker_approve",
                        {"request_id": rid(), "job_id": jid,
                         "attempt_id": job["active_attempt_id"],
                         "approval_id": pa["approval_id"],
                         "challenge": "B" * 43, "decision": "approve",
                         "user_decision_ref": "ui/bad"})
        self.assertEqual(bad["error"]["code"], "APPROVAL_INVALID")
        # right challenge -> recorded once; job resumes and completes
        ok = self.call("worker_approve",
                       {"request_id": rid(), "job_id": jid,
                        "attempt_id": job["active_attempt_id"],
                        "approval_id": pa["approval_id"],
                        "challenge": pa["challenge"],
                        "decision": "approve",
                        "user_decision_ref": "ui/ok"})
        assert_envelope(self, ok, "worker_approve")
        self.assertTrue(ok["data"]["decision_recorded"])
        self.wait_for(jid, ("completed",))
        # replay of the accepted approve returns its receipt
        # (same request_id -> journal replay path)

    def test_expired_and_double_decision(self):
        self.backend.steps = [("permission", PERM), outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("waiting_approval",))
        st = self.call("worker_status", {"job_id": jid})
        job = st["data"]["jobs"][0]
        pa = job["pending_approvals"][0]
        self.clock.advance(sup_mod.APPROVAL_TTL_S + 2)
        exp = self.call("worker_approve",
                        {"request_id": rid(), "job_id": jid,
                         "attempt_id": job["active_attempt_id"],
                         "approval_id": pa["approval_id"],
                         "challenge": pa["challenge"],
                         "decision": "approve",
                         "user_decision_ref": "ui/late"})
        self.assertEqual(exp["error"]["code"], "APPROVAL_EXPIRED")
        st = self.call("worker_status", {"job_id": jid})
        self.call("worker_cancel",
                  {"request_id": rid(), "job_id": jid,
                   "expected_attempt_id":
                       st["data"]["jobs"][0]["active_attempt_id"]})
        self.wait_for(jid, ("cancelled", "needs_attention"))
        if self.job_state(jid) == "needs_attention":
            # runner parked on expired approval -> needs_attention; a
            # second cancel carries the reconcile proof
            self.call("worker_cancel",
                      {"request_id": rid(), "job_id": jid,
                       "expected_attempt_id":
                           st["data"]["jobs"][0]["active_attempt_id"]})
            self.wait_for(jid, ("cancelled",), timeout=6)


class ErrorCodeCoverageRest(P4TestCase):
    """The remaining codes; combined with ErrorCodeCoverage every one of
    the 30 has fired from a real path in this module."""

    def _err(self, tool, args, code):
        env = self.call(tool, args)
        self.assertIn("error", env, f"expected {code}")
        self.assertEqual(env["error"]["code"], code, str(env)[:250])
        return env

    def test_busy_state_codes(self):
        self.backend.steps = [("hang", None)] * 400 + [
            outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        att = self.sup._job_row(jid)["active_attempt_id"]
        # followup while the job is active: JOB_BUSY (not a queue)
        self._err("worker_followup",
                  {"request_id": rid(), "user_request_ref": "r/f",
                   "job_id": jid, "expected_attempt_id": att,
                   "mode": "continue",
                   "message": "more"}, "JOB_BUSY")
        # select_model is only for waiting_model_selection
        self._err("worker_followup",
                  {"request_id": rid(), "user_request_ref": "r/s",
                   "job_id": jid, "expected_attempt_id": att,
                   "mode": "select_model", "model": "fake/go",
                   "message": "switch please",
                   "user_decision_ref": "ui/x"}, "JOB_BUSY")
        # stale attempt guards on cancel and followup
        self._err("worker_cancel",
                  {"request_id": rid(), "job_id": jid,
                   "expected_attempt_id": fake_att_id()},
                  "STALE_ATTEMPT")
        self._err("worker_followup",
                  {"request_id": rid(), "user_request_ref": "r/t",
                   "job_id": jid, "expected_attempt_id": fake_att_id(),
                   "mode": "retry", "message": "retry"},
                  "STALE_ATTEMPT")
        # unconfirmed cancel -> needs_attention; second cancel refuses
        self.backend.cancel_confirmed = False
        self.call("worker_cancel",
                  {"request_id": rid(), "job_id": jid,
                   "expected_attempt_id": att})
        self.wait_for(jid, ("needs_attention",))
        self.assertTrue((self.sup._job_row(jid)["attention_reason"]
                         or "").startswith("CANCEL_UNCONFIRMED"))
        self._err("worker_cancel",
                  {"request_id": rid(), "job_id": jid,
                   "expected_attempt_id": att}, "CANCEL_UNCONFIRMED")
        self.backend.cancel_confirmed = True

    def test_catalog_internal(self):
        self.backend.probe_error = ValueError("probe exploded")
        try:
            self._err("worker_catalog", {"refresh": True}, "INTERNAL")
        finally:
            self.backend.probe_error = None

    def test_cursor_codes(self):
        for _ in range(2):
            self.completed_start(rid())
        page = self.call("worker_status", {"limit": 1})
        cur = page["data"]["next_cursor"]
        self.assertIsNotNone(cur, "need a paginated cursor")
        # cursor bound to a filter set cannot cross filters
        self._err("worker_status",
                  {"states": ["completed"], "cursor": cur, "limit": 1},
                  "CURSOR_INVALID")
        # result-revision binding: cursor from the current revision goes
        # stale once a followup publishes a newer revision view
        jid = page["data"]["jobs"][0]["job_id"]
        stale = self.sup._cursor_box.make({
            "principal": self.uid_principal(), "tool": "worker_result",
            "section": "summary", "job": jid, "attempt": None,
            "filters_hash": "", "snapshot_id": None,
            "next_offset": 1, "result_revision": 99,
            "expires_at": self.clock.time() + 60})
        self._err("worker_result", {"job_id": jid, "section": "summary",
                                    "cursor": stale}, "STALE_RESULT")
        # TTL expiry
        aged = self.sup._cursor_box.make({
            "principal": self.uid_principal(), "tool": "worker_status",
            "section": None, "job": None, "attempt": None,
            "filters_hash": "", "snapshot_id": None, "next_offset": 1,
            "result_revision": None,
            "expires_at": self.clock.time() + 1})
        self.clock.advance(2)
        self._err("worker_status", {"cursor": aged}, "CURSOR_EXPIRED")

    def test_followup_reconcile_codes(self):
        jid = self.completed_start(rid())
        att = self.sup._job_row(jid)["active_attempt_id"]
        self.backend.reconcile_result = "pending"
        self._err("worker_followup",
                  {"request_id": rid(), "user_request_ref": "r/p",
                   "job_id": jid, "expected_attempt_id": att,
                   "mode": "continue", "message": "again"},
                  "APPROVAL_REQUIRED")
        self.backend.reconcile_result = "idle"
        self.backend.reconcile_error = sup_mod.BackendTimeout("slow")
        self._err("worker_followup",
                  {"request_id": rid(), "user_request_ref": "r/to",
                   "job_id": jid, "expected_attempt_id": att,
                   "mode": "continue", "message": "again"},
                  "BACKEND_TIMEOUT")
        self.backend.reconcile_error = None
        self.backend.reconcile_result = "idle"

    def test_workspace_changed(self):
        # accept without dispatch, baseline moves, dispatch must refuse
        self.sup.config.dispatch_enabled = False
        try:
            a = self.start_args(rid())
            env = self.call("worker_start", a)
            self.assertIn("data", env, str(env)[:200])
            jid = env["data"]["job"]["job_id"]
            v = sup_mod.validate_start(a)
            self.sup._validate_repo_at_start(v, v["workspace"]
                                             ["repository"])
            from _wt_support import commit_all, write
            write(self.repo / "moved.txt", "baseline moved\n")
            commit_all(self.repo, "move baseline")
            req_id = a["request_id"]
            self.sup._start_runner(self.uid_principal(), req_id, jid, v)
            self.wait_for(jid, ("failed",))
            st = self.call("worker_status", {"job_id": jid})
            self.assertEqual(st["data"]["jobs"][0]["state"], "failed")
        finally:
            self.sup.config.dispatch_enabled = True

    def test_ambiguous_and_evidence_and_storage(self):
        jid = self.completed_start(rid())
        att = self.sup._job_row(jid)["active_attempt_id"]
        # an ambiguous crash-window request blocks any new dispatch
        with self.sup._db():
            self.sup.store.conn.execute(
                "UPDATE requests SET phase = 'ambiguous' WHERE job_id = ?",
                (jid,))
        self._err("worker_followup",
                  {"request_id": rid(), "user_request_ref": "r/a",
                   "job_id": jid, "expected_attempt_id": att,
                   "mode": "retry", "message": "retry"},
                  "DISPATCH_AMBIGUOUS")

    def test_evidence_incomplete(self):
        from hina_opencode_worker import artifacts as art_mod
        jid_probe = self.completed_start(rid())  # healthy one first

        def boom(*a, **k):
            raise art_mod.ArtifactError("ARTIFACT_IO", "disk gone")
        saved = self.sup._write_result_artifacts
        self.sup._write_result_artifacts = boom
        try:
            self.backend.steps = [outcome_completed()]
            jid = self.call("worker_start",
                            self.start_args(rid()))["data"]["job"]["job_id"]
            self.wait_for(jid, ("needs_attention",))
            reason = self.sup._job_row(jid)["attention_reason"] or ""
            self.assertTrue(reason.startswith("EVIDENCE_INCOMPLETE"),
                            reason)
        finally:
            self.sup._write_result_artifacts = saved

    def test_storage_full(self):
        jid = self.completed_start(rid())  # ensure schema has activity
        pc = self.sup.store.diagnostics()["page_count"]
        saved = self.sup.store.set_max_page_count(pc + 1)
        try:
            # a multi-page payload over a 1-page headroom hits real
            # SQLITE_FULL (same mechanism as P1 T04, WAL included)
            self._err("worker_start",
                      self.start_args(rid(), goal="x" * 12000),
                      "STORAGE_FULL")
        finally:
            self.sup.store.set_max_page_count(saved + 10000)


if __name__ == "__main__":
    unittest.main()
