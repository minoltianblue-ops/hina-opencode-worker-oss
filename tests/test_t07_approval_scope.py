"""T07 — approval scope/attempt/TTL/replay/challenge forgery and a wrong
UI channel must all be refused (M1_WORK_PACKAGES.md T07).

Contract: docs/contracts/MCP.md §7 (challenge = base64url HMAC-SHA256 of
the canonical payload; 15-min TTL; replay journal consulted BEFORE
TTL/state rechecks; unverified channel blocks). Backend reply shapes are
the OBSERVED once/reject API (run 20261018T103444Z); the grant objects
themselves are OURS, not OpenCode output.
"""
import json
import time
import unittest

from _v2_support import FakeApiRunner, SYNTH

from hina_opencode_worker.events import scope_sha256
from hina_opencode_worker.opencode_v2 import (ApprovalError, ApprovalGrant,
                                              ApprovalJournal,
                                              OpenCodeV2Adapter,
                                              SessionApiTransport,
                                              compute_challenge,
                                              validate_and_reply)

KEY = b"\x01" * 32  # test key; production reads keys/approval.key elsewhere
SID = "ses_FAKE_synthetic"
REQ = json.loads((SYNTH / "permission-request.json").read_text())


def make_grant(**over) -> ApprovalGrant:
    now = time.time()
    base = dict(approval_id="apr_1", attempt_id="att_1",
                backend_request_id=REQ["id"], principal="user:owner",
                scope_sha256=scope_sha256(REQ), generation=1,
                expires_at=now + 900, channel_verified=True,
                decision="once", challenge=None)
    base.update(over)
    g = ApprovalGrant(**base)
    if g.challenge is None and g.channel_verified:
        g = ApprovalGrant(**{**base,
                             "challenge": compute_challenge(KEY, g)})
    return g


def adapter_for(pending=None, reply_exit=0):
    pending = pending if pending is not None else [REQ]
    runner = FakeApiRunner({
        ("GET", f"/api/session/{SID}/permission"): {"body": {"data": pending}},
        ("POST", f"/api/session/{SID}/permission/{REQ['id']}/reply"):
            {"exit_code": reply_exit, "body": None},
    })
    return OpenCodeV2Adapter(SessionApiTransport(
        "/nonexistent-opencode", "http://127.0.0.1:1/x",
        process_runner=runner)), runner


class TestHappyAndReplay(unittest.TestCase):
    def test_valid_grant_replies_once_and_journals(self):
        ad, runner = adapter_for()
        j = ApprovalJournal()
        r = validate_and_reply(ad, SID, make_grant(), key=KEY,
                               current_attempt_id="att_1", journal=j)
        self.assertTrue(r.ok)
        self.assertEqual(runner.calls_for(
            "POST", f"/api/session/{SID}/permission/{REQ['id']}/reply"), 1)

    def test_replay_returns_journal_result_without_reexecuting(self):
        ad, runner = adapter_for()
        j = ApprovalJournal()
        validate_and_reply(ad, SID, make_grant(), key=KEY,
                           current_attempt_id="att_1", journal=j)
        r2 = validate_and_reply(ad, SID, make_grant(), key=KEY,
                                current_attempt_id="att_1", journal=j)
        self.assertEqual(r2.method, "REPLAY")
        self.assertTrue(r2.body["replayed"])
        self.assertEqual(runner.calls_for(
            "POST", f"/api/session/{SID}/permission/{REQ['id']}/reply"), 1)

    def test_replay_precedes_ttl_recheck(self):
        # §7: an already-accepted request is answered from the journal
        # BEFORE TTL/state rechecks — even after expiry.
        ad, runner = adapter_for()
        j = ApprovalJournal()
        g = make_grant()
        validate_and_reply(ad, SID, g, key=KEY, current_attempt_id="att_1",
                           journal=j)
        expired = ApprovalGrant(**{**g.__dict__, "expires_at": time.time() - 1})
        expired = ApprovalGrant(**{**expired.__dict__,
                                   "challenge": compute_challenge(KEY, expired)})
        r = validate_and_reply(ad, SID, expired, key=KEY,
                               current_attempt_id="att_1", journal=j,
                               now=time.time())
        self.assertEqual(r.method, "REPLAY")

    def test_challenge_not_stored_only_its_sha(self):
        g = make_grant()
        self.assertIsNotNone(g.challenge)
        self.assertNotEqual(g.challenge_sha256, g.challenge)
        self.assertEqual(len(g.challenge_sha256), 64)


class TestRefusals(unittest.TestCase):
    def _fail(self, grant, ad=None, runner=None, **kw):
        ad = ad or OpenCodeV2Adapter(SessionApiTransport(
            "/bin", "http://x", process_runner=FakeApiRunner({})))
        with self.assertRaises(ApprovalError) as cm:
            validate_and_reply(ad, SID, grant, key=KEY,
                               current_attempt_id=kw.pop("attempt", "att_1"),
                               journal=ApprovalJournal(), **kw)
        return cm.exception

    def test_unverified_channel_blocked(self):
        g = ApprovalGrant(approval_id="a", attempt_id="att_1",
                          backend_request_id=REQ["id"], principal="p",
                          scope_sha256=scope_sha256(REQ), generation=1,
                          expires_at=time.time() + 900,
                          channel_verified=False, decision="once",
                          challenge=None)
        e = self._fail(g)
        self.assertEqual(e.code, "APPROVAL_CHANNEL_UNAVAILABLE")

    def test_expired(self):
        e = self._fail(make_grant(expires_at=time.time() - 5),
                       now=time.time())
        self.assertEqual(e.code, "APPROVAL_EXPIRED")

    def test_stale_attempt(self):
        e = self._fail(make_grant(attempt_id="att_OLD"), attempt="att_1")
        self.assertEqual(e.code, "STALE_ATTEMPT")

    def test_forged_challenge_rejected(self):
        forged = make_grant(challenge="Zm9yZ2Vk")  # not the real HMAC
        ad, _ = adapter_for()
        e = self._fail(forged, ad=ad)
        self.assertEqual(e.code, "APPROVAL_INVALID")

    def test_challenge_from_wrong_key_rejected(self):
        g = make_grant()
        wrong = compute_challenge(b"\x02" * 32, g)
        e = self._fail(make_grant(challenge=wrong), ad=adapter_for()[0])
        self.assertEqual(e.code, "APPROVAL_INVALID")

    def test_scope_drift_requires_new_confirmation(self):
        moved = {**REQ, "resources": ["outside/OTHER.txt"]}
        ad, runner = adapter_for(pending=[moved])
        e = self._fail(make_grant(), ad=ad)
        self.assertEqual(e.code, "APPROVAL_INVALID")
        self.assertIn("scope", e.detail.get("reason", ""))
        self.assertEqual(runner.calls_for(
            "POST", f"/api/session/{SID}/permission/{REQ['id']}/reply"), 0)

    def test_request_no_longer_pending(self):
        ad, _ = adapter_for(pending=[])
        e = self._fail(make_grant(), ad=ad)
        self.assertEqual(e.code, "APPROVAL_INVALID")

    def test_always_decision_never_sent(self):
        ad, runner = adapter_for()
        e = self._fail(make_grant(decision="always"), ad=ad)
        self.assertEqual(e.code, "APPROVAL_INVALID")
        self.assertEqual(runner.calls_for(
            "POST", f"/api/session/{SID}/permission/{REQ['id']}/reply"), 0)
        with self.assertRaises(ValueError):
            ad.reply_permission(SID, REQ["id"], "always")

    def test_reject_decision_is_allowed(self):
        ad, runner = adapter_for()
        g = make_grant(decision="reject")
        g = ApprovalGrant(**{**g.__dict__,
                             "challenge": compute_challenge(KEY, g)})
        r = validate_and_reply(ad, SID, g, key=KEY, current_attempt_id="att_1",
                               journal=ApprovalJournal())
        self.assertTrue(r.ok)


class TestCredentialHygiene(unittest.TestCase):
    def test_password_never_in_argv(self):
        runner = FakeApiRunner({("GET", "/api/session/active"):
                                {"body": {"data": {}}}})
        import os
        os.environ["OPENCODE_PASSWORD"] = "sekret-value"
        try:
            t = SessionApiTransport("/bin", "http://x",
                                    process_runner=runner)
            t.call("GET", "/api/session/active")
            for c in runner.calls:
                self.assertNotIn("sekret-value", json.dumps(c["argv"]))
                self.assertEqual(c["env"].get("OPENCODE_PASSWORD"),
                                 "sekret-value")  # env only, as observed
        finally:
            del os.environ["OPENCODE_PASSWORD"]


if __name__ == "__main__":
    unittest.main()
