"""U-C1..U-C3 — M3 N2 per-task MCP/skill allowlist (D6).

U-C1 off-allowlist refused: default-deny is absolute — with no grant
  nothing is usable; off-list MCPs/skills refuse with auto_grant
  False; only registry-known, enabled ids can ever be granted; a
  withdrawal after granting still refuses.
U-C2 skill scoping (least-privilege): a skill reaches an MCP only if
  the task grants it AND the skill declares it — it cannot exceed
  its declared needs, and it cannot use its needs to reach MCPs the
  task was not granted.
U-C3 refusal logged: every check() outcome (allow, deny) and every
  grant lands in an append-only audit log with its reason; refusals
  persist, are per-task, and the tables reject UPDATE/DELETE.

Registry is the shipped CapabilityRegistry throughout; the policy is
duck-typed against get_mcp/get_skill, exactly as a future wiring
would be.
"""
from __future__ import annotations

import datetime
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hina_opencode_worker import capabilities
from hina_opencode_worker.capabilities import (
    CapabilityPolicy, CapabilityPolicyError, CapabilityRegistry)

CLOCK = 1764000000.0
STAMP = datetime.datetime.fromtimestamp(
    CLOCK, datetime.timezone.utc).isoformat(timespec="seconds")


def registry() -> CapabilityRegistry:
    r = CapabilityRegistry()
    r.add_mcp("srv.alpha")
    r.add_mcp("srv.beta")
    r.add_mcp("srv.gamma")
    r.add_skill("sk.narrow", needs=("srv.alpha",))
    r.add_skill("sk.wide", needs=("srv.alpha", "srv.beta"))
    return r


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hina-uc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "capabilities.sqlite"
        self.registry = registry()
        self.policy = self.new_policy()

    def new_policy(self, registry=None):
        p = CapabilityPolicy(registry or self.registry, db_path=self.db,
                             clock=lambda: CLOCK)
        self.addCleanup(p.close)
        return p

    def grant(self, task="t1", mcps=(), skills=()):
        return self.policy.grant(
            task, mcps=mcps, skills=skills,
            granted_by="user:owner", user_decision_ref="ui/1")

    def refusal(self, fn, *a, **kw):
        with self.assertRaises(CapabilityPolicyError) as cm:
            fn(*a, **kw)
        return cm.exception


class U1DefaultDeny(PolicyTestCase):
    def test_no_grant_default_deny_both_kinds(self):
        for kind, rid in (("mcp", "srv.alpha"), ("skill", "sk.narrow")):
            exc = self.refusal(self.policy.check, "t1", kind, rid)
            self.assertEqual(exc.code, "CAPABILITY_DENIED")
            self.assertEqual(exc.details["reason_code"], "no_active_grant")
            self.assertFalse(exc.details["auto_grant"])
            self.assertTrue(exc.details["requires_user_decision"])
            self.assertFalse(exc.retryable)

    def test_off_allowlist_mcp_refused_and_on_list_allowed(self):
        g = self.grant(mcps=["srv.alpha"])
        rec = self.policy.check("t1", "mcp", "srv.alpha")
        self.assertEqual(rec.event, "allow")
        self.assertEqual(rec.grant_id, g.grant_id)
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.beta")
        self.assertEqual(exc.details["reason_code"], "mcp_not_allowed")
        self.assertEqual(exc.details["resource_id"], "srv.beta")
        self.assertFalse(exc.details["auto_grant"])

    def test_off_allowlist_skill_refused(self):
        self.grant(skills=["sk.narrow"])
        self.policy.check("t1", "skill", "sk.narrow")
        exc = self.refusal(self.policy.check, "t1", "skill", "sk.wide")
        self.assertEqual(exc.details["reason_code"], "skill_not_allowed")

    def test_empty_grant_revokes_all(self):
        self.grant(mcps=["srv.alpha"], skills=["sk.narrow"])
        self.grant()  # revocation is itself a user decision
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.alpha")
        self.assertEqual(exc.details["reason_code"], "mcp_not_allowed")

    def test_latest_grant_wins(self):
        self.grant(mcps=["srv.alpha"])
        self.grant(mcps=["srv.beta"])
        self.refusal(self.policy.check, "t1", "mcp", "srv.alpha")
        self.policy.check("t1", "mcp", "srv.beta")

    def test_grant_covers_only_known_enabled_ids(self):
        exc = self.refusal(self.policy.grant, "t1", mcps=["srv.nowhere"],
                           granted_by="user:owner",
                           user_decision_ref="ui/1")
        self.assertEqual(exc.details["reason_code"],
                         "registry_unknown_or_disabled")
        exc = self.refusal(self.policy.grant, "t1", skills=["sk.ghost"],
                           granted_by="user:owner",
                           user_decision_ref="ui/1")
        self.assertEqual(exc.details["reason_code"],
                         "registry_unknown_or_disabled")
        self.assertIsNone(self.policy.active_grant("t1"))

    def test_withdrawal_after_grant_refuses(self):
        self.grant(mcps=["srv.alpha"], skills=["sk.narrow"])
        self.registry.set_enabled("mcp", "srv.alpha", False)
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.alpha")
        self.assertEqual(exc.details["reason_code"], "registry_withdrawn")
        self.registry.set_enabled("skill", "sk.narrow", False)
        exc = self.refusal(self.policy.check, "t1", "skill", "sk.narrow")
        self.assertEqual(exc.details["reason_code"], "registry_withdrawn")

    def test_invalid_arguments_refused(self):
        for a, kw in ((("  ", "mcp", "srv.alpha"), {}),
                      (("t1", "tool", "srv.alpha"), {}),
                      (("t1", "skill", "sk.narrow"), {"via": "sk.wide"}),
                      (("t1", "mcp", "bad id"), {})):
            exc = self.refusal(self.policy.check, *a, **kw)
            self.assertEqual(exc.code, "INVALID_ARGUMENT")
        exc = self.refusal(self.policy.grant, "t1", mcps="srv.alpha",
                           granted_by="user:owner",
                           user_decision_ref="ui/1")
        self.assertEqual(exc.code, "INVALID_ARGUMENT")
        exc = self.refusal(self.policy.grant, "t1", mcps=["srv.alpha"],
                           granted_by="  ", user_decision_ref="ui/1")
        self.assertEqual(exc.code, "INVALID_ARGUMENT")

    def test_plan_none_until_grant(self):
        self.assertIsNone(self.policy.plan("t1"))
        g = self.grant(mcps=["srv.alpha"], skills=["sk.narrow"])
        plan = self.policy.plan("t1")
        self.assertEqual(plan.grant_id, g.grant_id)
        self.assertTrue(plan.allows_mcp("srv.alpha"))
        self.assertFalse(plan.allows_mcp("srv.beta"))
        self.assertTrue(plan.allows_skill("sk.narrow"))


class U2SkillLeastPrivilege(PolicyTestCase):
    def setUp(self):
        super().setUp()
        # task allows alpha+beta directly, and sk.narrow (declares
        # ONLY alpha).
        self.grant(mcps=["srv.alpha", "srv.beta"], skills=["sk.narrow"])

    def test_skill_uses_granted_declared_mcp(self):
        rec = self.policy.check("t1", "mcp", "srv.alpha", via="sk.narrow")
        self.assertEqual(rec.event, "allow")
        self.assertEqual(rec.via_skill, "sk.narrow")

    def test_skill_cannot_exceed_declared_needs(self):
        # beta is granted to the task but sk.narrow never declared it.
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.beta",
                           via="sk.narrow")
        self.assertEqual(exc.details["reason_code"], "skill_out_of_scope")
        self.assertEqual(exc.details["via_skill"], "sk.narrow")

    def test_direct_mcp_use_does_not_need_skill(self):
        # beta is off-limits THROUGH the skill, usable directly.
        self.policy.check("t1", "mcp", "srv.beta")

    def test_skill_cannot_self_escalate_task_scope(self):
        # sk.wide declares beta, but the task never granted beta to
        # any use via sk.wide — needs never expand the task grant.
        self.grant(mcps=["srv.alpha"], skills=["sk.wide"])
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.beta",
                           via="sk.wide")
        self.assertEqual(exc.details["reason_code"], "mcp_not_allowed")

    def test_via_skill_must_be_granted(self):
        # sk.wide is registered and beta is task-granted, but the
        # call must still travel through a GRANTED skill.
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.beta",
                           via="sk.wide")
        self.assertEqual(exc.details["reason_code"], "skill_not_allowed")

    def test_withdrawn_via_skill_refuses(self):
        self.grant(mcps=["srv.alpha"], skills=["sk.wide"])
        self.registry.set_enabled("skill", "sk.wide", False)
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.alpha",
                           via="sk.wide")
        self.assertEqual(exc.details["reason_code"], "registry_withdrawn")

    def test_skill_with_unregistered_needs_still_denied(self):
        self.registry.add_skill("sk.mystery", needs=("srv.ghost",))
        self.grant(mcps=["srv.alpha"], skills=["sk.mystery"])
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.ghost",
                           via="sk.mystery")
        self.assertEqual(exc.details["reason_code"], "mcp_not_allowed")
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.alpha",
                           via="sk.mystery")
        self.assertEqual(exc.details["reason_code"], "skill_out_of_scope")

    def test_plan_predicates_agree_with_check(self):
        plan = self.policy.plan("t1")
        self.assertTrue(plan.allows_mcp("srv.alpha", via="sk.narrow"))
        self.assertFalse(plan.allows_mcp("srv.beta", via="sk.narrow"))
        self.assertTrue(plan.allows_mcp("srv.beta"))
        self.assertFalse(plan.allows_skill("sk.ghost"))
        # a pure False must be the SAME refusal check() gives
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.beta",
                           via="sk.narrow")
        self.assertEqual(exc.details["reason_code"], "skill_out_of_scope")


class U3RefusalAudit(PolicyTestCase):
    def test_every_check_refusal_is_logged_in_order(self):
        # beta is task-granted but NOT declared by sk.narrow, so the
        # via-path refuses with the distinct skill_out_of_scope.
        self.grant(mcps=["srv.alpha", "srv.beta"], skills=["sk.narrow"])
        self.refusal(self.policy.check, "t1", "mcp", "srv.gamma")
        self.refusal(self.policy.check, "t1", "mcp", "srv.beta",
                     via="sk.narrow")
        self.refusal(self.policy.check, "t1", "skill", "sk.wide")
        rows = self.policy.refusals("t1")
        self.assertEqual([r.reason_code for r in rows],
                         ["mcp_not_allowed", "skill_out_of_scope",
                          "skill_not_allowed"])
        self.assertEqual([r.event for r in rows], ["deny"] * 3)
        self.assertEqual([r.kind for r in rows],
                         ["mcp", "mcp", "skill"])
        self.assertEqual(rows[1].via_skill, "sk.narrow")
        self.assertEqual({r.at for r in rows}, {STAMP})
        self.assertEqual({r.task_id for r in rows}, {"t1"})

    def test_grant_allow_deny_events_all_audited(self):
        g = self.grant(mcps=["srv.alpha"])
        self.policy.check("t1", "mcp", "srv.alpha")
        self.refusal(self.policy.check, "t1", "mcp", "srv.beta")
        self.assertEqual([d.event for d in self.policy.audit("t1")],
                         ["grant", "allow", "deny"])
        rows = self.policy.audit("t1")
        self.assertEqual(rows[0].grant_id, g.grant_id)
        self.assertEqual(rows[1].grant_id, g.grant_id)
        self.assertEqual(rows[2].grant_id, g.grant_id)

    def test_refusal_details_match_audit_entry(self):
        self.grant(mcps=["srv.alpha"])
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.beta")
        row = self.policy.refusals("t1")[-1]
        self.assertEqual(exc.details["reason_code"], row.reason_code)
        self.assertEqual(exc.details["resource_id"], row.resource_id)
        entries = [d.entry_id for d in self.policy.audit("t1")]
        self.assertEqual(len(entries), len(set(entries)))

    def test_audit_and_grants_persist_across_reopen(self):
        self.grant(mcps=["srv.alpha"])
        self.refusal(self.policy.check, "t1", "mcp", "srv.beta")
        self.policy.close()
        reopened = self.new_policy()
        self.assertIsNotNone(reopened.active_grant("t1"))
        self.assertEqual(
            [r.reason_code for r in reopened.refusals("t1")],
            ["mcp_not_allowed"])
        self.assertEqual(
            [g.decided_at for g in reopened.grants("t1")], [STAMP])

    def test_audit_log_is_append_only(self):
        self.grant(mcps=["srv.alpha"])
        self.refusal(self.policy.check, "t1", "mcp", "srv.beta")
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        for stmt in ("UPDATE capability_audit SET reason_code='x'",
                     "DELETE FROM capability_audit",
                     "UPDATE capability_grants SET mcps='[]'",
                     "DELETE FROM capability_grants"):
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(stmt)

    def test_audit_scoped_per_task(self):
        self.grant(mcps=["srv.alpha"])
        self.grant(task="t2", mcps=["srv.beta"])
        self.refusal(self.policy.check, "t1", "mcp", "srv.beta")
        self.assertEqual(len(self.policy.refusals("t1")), 1)
        self.assertEqual(self.policy.refusals("t2"), ())

    def test_refused_grant_attempt_is_logged(self):
        exc = self.refusal(self.policy.grant, "t1", mcps=["srv.nowhere"],
                           granted_by="user:owner",
                           user_decision_ref="ui/1")
        self.assertEqual(exc.details["reason_code"],
                         "registry_unknown_or_disabled")
        rows = self.policy.refusals("t1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "grant")
        self.assertEqual(rows[0].resource_id, "srv.nowhere")
        self.assertEqual(self.policy.grants("t1"), ())

    def test_grants_history_append_only(self):
        g1 = self.grant(mcps=["srv.alpha"])
        g2 = self.grant(mcps=["srv.beta"])
        trail = self.policy.grants("t1")
        self.assertEqual([g.grant_id for g in trail],
                         [g1.grant_id, g2.grant_id])
        self.assertEqual(self.policy.active_grant("t1").grant_id,
                         g2.grant_id)
        self.assertEqual(trail[0].user_decision_ref, "ui/1")
        self.assertEqual(trail[0].granted_by, "user:owner")

    def test_error_envelope_shape(self):
        exc = self.refusal(self.policy.check, "t1", "mcp", "srv.alpha")
        err = exc.to_error()
        self.assertEqual(sorted(err),
                         ["code", "details", "message", "retryable"])
        self.assertEqual(err["code"], "CAPABILITY_DENIED")
        self.assertFalse(err["retryable"])
        self.assertEqual(err["details"]["disposition"], "refused")
        self.assertFalse(err["details"]["auto_grant"])
        self.assertTrue(err["details"]["requires_user_decision"])
        # the module surface stays the documented one
        self.assertEqual(
            sorted(capabilities.__all__),
            ["CapabilityPolicy", "CapabilityPolicyError",
             "CapabilityRegistry", "DecisionRecord", "GrantRecord",
             "TaskCapabilities"])


if __name__ == "__main__":
    unittest.main()
