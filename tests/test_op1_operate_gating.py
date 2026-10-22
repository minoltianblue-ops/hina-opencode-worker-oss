"""T-OP1..T-OP3 — M2-Q3 operate profiles (WORKSPACE.md ch7).

Real service/system configuration cannot be Worktree-isolated, so
operate needs a separate profile (operate.yaml: explicit scope +
approval record + recovery plan) and fail-closed supervisor gating.
These tests cover the DRY-RUN paths ONLY — by design nothing here
touches a real service/tunnel/V1/cokacremote and the gate has no code
path that could.

T-OP1 scope refusal  — no profile / no approval record / scope miss /
       ambiguous profile / real mode / client surface: every refusal
       uses a frozen contract error code.
T-OP2 dry-run recovery — an approved scope plans inertly (executed=false,
       zero backend/fs effect) and always carries the recovery plan; a
       profile without a recovery plan approves nothing; binding needs
       a live attempt.
T-OP3 approval record — durable per-attempt approval records (audit
       journal), per-attempt TTL attempt binding (cross-attempt
       STALE_ATTEMPT, TTL APPROVAL_EXPIRED), approval-window expiry and
       scope drift after approval.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from _p4_support import P4TestCase, fake_att_id, rid
from hina_opencode_worker import operate as opmod
from hina_opencode_worker.supervisor import (
    ERROR_CODES, OPERATE_ATTEMPT_TTL_S)

DEFAULT_SCOPE = [{"id": "restart-svc", "action": "service.restart",
                  "target": "com.example.svc"}]


def _z(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def normalized(scope: list[dict]) -> list[dict]:
    return [{"id": e["id"], "action": e["action"], "target": e["target"],
             "note": e.get("note", "")} for e in scope]


def profile_text(*, scope=None, now=None, valid=True, recovery=True,
                 sha=None):
    """Write-compatible YAML for the strict subset parser. valid=False
    drops the approval record; recovery=False drops the recovery plan."""
    scope = scope if scope is not None else DEFAULT_SCOPE
    now = time.time() if now is None else now
    lines = ["profile: operate", "version: 1", "scope:"]
    for e in scope:
        lines.append(f"  - id: {e['id']}")
        lines.append(f"    action: {e['action']}")
        lines.append(f"    target: {e['target']}")
        if e.get("note"):
            lines.append(f"    note: {e['note']}")
    if valid:
        lines += [
            "approval:",
            "  approved_by: user:operator-jh",
            "  user_decision_ref: chat/2026/operate/1",
            f"  approved_at: {_z(now - 3600)}",
            f"  expires_at: {_z(now + 86400)}",
            f"  scope_sha256: {sha or opmod.scope_sha256(normalized(scope))}",
        ]
    if recovery:
        lines += ["recovery_plan:",
                  "  - step: restore previous config from backup copy",
                  "  - step: unload the agent and re-check service state"]
    return "\n".join(lines) + "\n"


class OperateBase(P4TestCase):
    """Supervisor with a running inspect job (live attempt) + an
    operate.yaml under the supervisor root."""

    def setUp(self):
        super().setUp()
        self._jobs: list[str] = []
        self.backend.steps = []
        env = self.call("worker_start",
                        self.start_args(profile="inspect"))
        self.assertIn("data", env, str(env))
        self.jid = env["data"]["job"]["job_id"]
        self.wait_for(self.jid, ("running",))
        self._jobs.append(self.jid)
        self.att = self.sup._job_row(self.jid)["active_attempt_id"]
        self.assertIsNotNone(self.att)
        self.profile_path = Path(self.sup.config.operate_profile_path)

    def tearDown(self):
        # drain live runners BEFORE the temp root disappears (UQ/W6
        # family pattern): addCleanup would run after tearDown, when the
        # DB is already gone.
        for jid in getattr(self, "_jobs", []):
            try:
                self.finish(jid)
            except Exception:
                pass
        end = time.monotonic() + 5.0
        while time.monotonic() < end and any(
                t.is_alive() for t in list(self.sup._runners.values())):
            time.sleep(0.05)
        super().tearDown()

    def write_profile(self, text: str) -> None:
        self.profile_path.write_text(text, encoding="utf-8")

    def operator(self, method, arguments=None):
        return self.raw({"client": "operator", "method": method,
                         "arguments": arguments or {}})

    def plan(self, action="service.restart", target="com.example.svc",
             attempt_id="__live__", dry_run=None):
        args = {"operation": {"action": action, "target": target}}
        if attempt_id != "__live__":
            args["attempt_id"] = attempt_id
        if dry_run is not None:
            args["dry_run"] = dry_run
        return self.operator("operate.plan", args)


class OP1ScopeRefusal(OperateBase):
    def test_op1_no_profile_refused(self):
        # capability not installed: nothing exists to approve operate
        env = self.plan()
        self.assertEqual(env["error"]["code"], "CAPABILITY_UNVERIFIED")
        self.assertIn("ch7", env["error"]["message"])

    def test_op1_missing_approval_record_refused(self):
        # profile without the approval record approves NOTHING
        self.write_profile(profile_text(valid=False))
        env = self.plan()
        self.assertEqual(env["error"]["code"], "APPROVAL_REQUIRED")
        self.assertIn("approval", env["error"]["message"])

    def test_op1_outside_scope_forbidden(self):
        self.write_profile(profile_text())
        env = self.plan(action="service.stop")
        self.assertEqual(env["error"]["code"], "FORBIDDEN")
        self.assertIn("scope", env["error"]["message"])
        # a target that is not the explicitly approved one: refused
        env2 = self.plan(target="com.example.other")
        self.assertEqual(env2["error"]["code"], "FORBIDDEN")
        # and the approved pair is accepted (so the above really is the
        # scope refusal, not a general failure)
        self.assertIn("data", self.plan())

    def test_op1_ambiguous_profile_refuses_everything(self):
        # glob target (ch8: explicit only), flow style, duplicate keys,
        # tabs — none of these may half-approve anything
        for bad in (
                profile_text(scope=[{"id": "x", "action": "config.apply",
                                     "target": "com.example.*"}]),
                "profile: operate\nversion: 1\napproval: {a: 1}\n",
                "profile: operate\nprofile: operate\n",
                "profile: operate\n\tversion: 1\n"):
            self.write_profile(bad)
            env = self.plan()
            self.assertEqual(env["error"]["code"], "APPROVAL_REQUIRED",
                             f"bad profile half-approved: {bad[:40]}")
        # parser-level certainty
        with self.assertRaises(opmod.OperateDenied):
            opmod.parse_simple_yaml("- a\n- b\n")

    def test_op1_real_mode_never_executed(self):
        self.write_profile(profile_text())
        env = self.plan(dry_run=False)
        self.assertEqual(env["error"]["code"], "FORBIDDEN")
        self.assertIn("separate user approval",
                      env["error"]["message"])
        self.assertFalse(env["error"]["retryable"])

    def test_op1_surface_reachable_by_operator_only(self):
        self.write_profile(profile_text())
        # mcp_proxy: frozen 8 tools only — operate.* is unknown to it
        denied = self.raw({"client": "mcp_proxy",
                           "tool": "operate.plan", "arguments": {}})
        self.assertEqual(denied["error"]["code"], "INVALID_ARGUMENT")
        # viewer: read-only allowlist never included operate
        v = self.raw({"client": "viewer", "method": "operate.plan",
                      "arguments": {}})
        self.assertEqual(v["error"]["code"], "FORBIDDEN")
        # operator may not invent methods
        exec_env = self.raw({"client": "operator",
                             "method": "operate.exec", "arguments": {}})
        self.assertEqual(exec_env["error"]["code"], "FORBIDDEN")
        # a foreign principal cannot pose as the local operator
        foreign = self.sup.dispatch(
            {"client": "operator", "method": "operate.status",
             "arguments": {}}, principal="local:999999")
        self.assertEqual(foreign["error"]["code"], "UNAUTHENTICATED")

    def test_op1_error_codes_are_frozen_contract_codes(self):
        # the operate surface must never mint a new error code (T13)
        self.write_profile(profile_text())
        envs = [self.plan(dry_run=False), self.plan(target="no.xe"),
                self.operator("operate.status"),
                self.operator("operate.revalidate",
                              {"record_id": "opr_" + "0" * 36,
                               "attempt_id": fake_att_id()})]
        codes = {e["error"]["code"] for e in envs if "error" in e}
        self.assertTrue(codes, "expected refusals in every probe")
        self.assertEqual(codes - set(ERROR_CODES), set(),
                         f"non-contract codes: {codes - set(ERROR_CODES)}")


class OP2DryRunRecovery(OperateBase):
    def _fs(self, root):
        return sorted(str(p.relative_to(root))
                      for p in Path(root).rglob("*"))

    def test_op2_dry_run_plan_is_inert_and_carries_recovery(self):
        self.write_profile(profile_text())
        before_root, before_repo = self._fs(self.root), self._fs(self.repo)
        started, replies = len(self.backend.started), len(
            self.backend.reply_log)
        env = self.plan(attempt_id=self.att)
        data = env["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["planned_actions"], [{
            "seq": 1, "action": "service.restart",
            "target": "com.example.svc", "effect": "DRY_RUN",
            "executed": False,
            "note": data["planned_actions"][0]["note"]}])
        self.assertIn("not implemented in M2",
                      data["planned_actions"][0]["note"])
        # recovery plan travels with every plan (ch7)
        self.assertEqual(len(data["recovery_plan"]), 2)
        self.assertIn("restore previous config", data["recovery_plan"][0])
        self.assertEqual(data["operation"]["scope_id"], "restart-svc")
        # purity: no backend call, no repo change, no state change
        self.assertEqual(len(self.backend.started), started)
        self.assertEqual(len(self.backend.reply_log), replies)
        self.assertEqual(self._fs(self.repo), before_repo)
        self.assertEqual(self.job_state(self.jid), "running")
        # only the operate audit journal may appear under the root
        added = set(self._fs(self.root)) - set(before_root)
        self.assertTrue(all(p == "operate" or p.startswith("operate/")
                            for p in added), str(added))

    def test_op2_no_recovery_plan_approves_nothing(self):
        self.write_profile(profile_text(recovery=False))
        env = self.plan()
        self.assertEqual(env["error"]["code"], "APPROVAL_REQUIRED")
        self.assertIn("recovery", env["error"]["message"])

    def test_op2_binding_requires_live_attempt(self):
        self.write_profile(profile_text())
        env = self.plan(attempt_id=fake_att_id())
        self.assertEqual(env["error"]["code"], "STALE_ATTEMPT")
        # an unbound plan is still a valid dry-run plan (binding is an
        # opt-in per-attempt lock, not a precondition)
        self.assertIn("data", self.plan(attempt_id=None))


class OP3ApprovalRecord(OperateBase):
    def test_op3_record_is_durable_and_attempt_bound(self):
        self.write_profile(profile_text())
        env = self.plan(attempt_id=self.att)
        binding = env["data"]["attempt_binding"]
        self.assertRegex(binding["record_id"],
                         r"^opr_[0-9a-f-]{36}$")
        self.assertEqual(binding["attempt_id"], self.att)
        self.assertEqual(
            binding["scope_sha256"],
            env["data"]["profile"]["scope_sha256"])
        self.assertTrue(binding["dry_run"])
        # per-attempt TTL: expires_at = issued + OPERATE_ATTEMPT_TTL_S
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        issued = datetime.strptime(binding["issued_at"], fmt)
        expires = datetime.strptime(binding["expires_at"], fmt)
        self.assertLessEqual(
            abs((expires - issued).total_seconds()
                - OPERATE_ATTEMPT_TTL_S), 1)
        # durable audit journal, mode 0600, no challenge/secret material
        journal = Path(self.sup.config.operate_journal_dir) / \
            "approvals.jsonl"
        self.assertTrue(journal.exists())
        self.assertEqual(oct(journal.stat().st_mode & 0o777), "0o600")
        line = journal.read_text(encoding="utf-8").strip().splitlines()[-1]
        rec = json.loads(line)["record"]
        self.assertEqual(rec["record_id"], binding["record_id"])
        self.assertNotIn("challenge", json.dumps(rec))
        # revalidate while valid + bound: idempotent success
        rv = self.operator("operate.revalidate",
                           {"record_id": binding["record_id"],
                            "attempt_id": self.att})
        self.assertEqual(rv["data"]["record_id"], binding["record_id"])
        # status surfaces the record count
        st = self.operator("operate.status")
        self.assertTrue(st["data"]["installed"])
        self.assertEqual(st["data"]["approval_records"], 1)

    def test_op3_cross_attempt_is_stale_and_ttl_expiry(self):
        self.write_profile(profile_text())
        binding = self.plan(attempt_id=self.att)["data"][
            "attempt_binding"]
        # the record must not travel to another attempt: cancel this
        # job (supervisor holds one current task) and start a second,
        # whose live attempt may not consume the first attempt's record
        self.finish(self.jid)
        env2 = self.call("worker_start", self.start_args(
            profile="inspect", request_id=rid(),
            goal="second job for attempt rotation"))
        self.assertIn("data", env2, str(env2))
        jid2 = env2["data"]["job"]["job_id"]
        self.wait_for(jid2, ("running",))
        self._jobs.append(jid2)
        att2 = self.sup._job_row(jid2)["active_attempt_id"]
        rv = self.operator("operate.revalidate",
                           {"record_id": binding["record_id"],
                            "attempt_id": att2})
        self.assertEqual(rv["error"]["code"], "STALE_ATTEMPT")
        # while still bound-valid it stays valid for ITS OWN attempt;
        # once the per-attempt TTL passes it is dead even there
        ok = self.operator("operate.revalidate",
                           {"record_id": binding["record_id"],
                            "attempt_id": self.att})
        self.assertEqual(ok["data"]["record_id"], binding["record_id"])
        self.clock.advance(OPERATE_ATTEMPT_TTL_S + 1)
        rv2 = self.operator("operate.revalidate",
                            {"record_id": binding["record_id"],
                             "attempt_id": self.att})
        self.assertEqual(rv2["error"]["code"], "APPROVAL_EXPIRED")

    def test_op3_approval_window_expired(self):
        now = time.time()
        # approved last month, window already closed
        text = profile_text(now=now - 5 * 86400)
        self.write_profile(text)
        env = self.plan()
        self.assertEqual(env["error"]["code"], "APPROVAL_EXPIRED")

    def test_op3_scope_drift_after_approval(self):
        self.write_profile(profile_text())
        self.assertIn("data", self.plan())
        # the scope is edited after approval but the approval record
        # keeps the OLD scope hash — silent scope expansion is refused
        drifted_scope = [
            {"id": "restart-svc", "action": "service.restart",
             "target": "com.example.svc"},
            {"id": "sneaky", "action": "config.apply",
             "target": "/etc/resolv.conf"}]
        self.write_profile(profile_text(
            scope=drifted_scope,
            sha=opmod.scope_sha256(normalized(DEFAULT_SCOPE))))
        env = self.plan()
        self.assertEqual(env["error"]["code"], "APPROVAL_INVALID")
        # even the originally approved pair is refused while drifting
        self.assertEqual(self.plan()["error"]["code"], "APPROVAL_INVALID")


if __name__ == "__main__":
    unittest.main()
