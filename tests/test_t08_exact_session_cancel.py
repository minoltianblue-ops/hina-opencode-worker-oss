"""T08 — cancel must target the EXACT session; an HTTP ack or a CLI-only
kill never confirms cancelled; other sessions must survive
(M1_WORK_PACKAGES.md T08, OPENCODE_V2.md §4).

Interrupt/active API shapes are OBSERVED (run 20261018T104948Z: POST
.../interrupt no body -> {"interrupted": true}; GET /api/session/active
data keyed by running session ids). The active-set transitions below are
SYNTHETIC simulations of that observation.
"""
import unittest

from _v2_support import FakeApiRunner

from hina_opencode_worker.events import VERDICT_UNKNOWN, judge_completion
from hina_opencode_worker.opencode_v2 import (ApiResponse,
                                              OpenCodeV2Adapter,
                                              SessionApiTransport)

SID = "ses_target_exact"
OTHER = "ses_other_survivor"


def build(active_by_call=None, *, interrupt_exit=0, session_exists=True,
          pending=None):
    """Scripted server: active_by_call is a list of active-id lists
    consumed per GET /api/session/active call (last one repeats)."""
    calls = {"active": 0}
    recorded: list[tuple[str, str]] = []

    responses = {
        ("POST", f"/api/session/{SID}/interrupt"):
            {"exit_code": interrupt_exit,
             "body": {"data": {"interrupted": True}}},
    }
    runner = FakeApiRunner(responses)

    def runner2(argv, env):
        recorded.append((argv[-2], argv[-1]))
        if argv[-2] == "GET" and argv[-1] == "/api/session/active":
            i = min(calls["active"], len(active_by_call) - 1)
            calls["active"] += 1
            return 0, __import__("json").dumps(
                {"data": {sid: {"x": 1} for sid in active_by_call[i]}}), ""
        if argv[-2] == "GET" and argv[-1] == f"/api/session/{SID}":
            if session_exists:
                return 0, __import__("json").dumps(
                    {"data": {"id": SID}}), ""
            return 1, "", "not found"
        if argv[-2] == "GET" and argv[-1] == \
                f"/api/session/{SID}/permission":
            return 0, __import__("json").dumps(
                {"data": pending or []}), ""
        return runner(argv, env)

    t = SessionApiTransport("/nonexistent-opencode", "http://127.0.0.1:1/x",
                            process_runner=runner2)
    return OpenCodeV2Adapter(t), recorded, calls


class TestExactSessionCancel(unittest.TestCase):
    def test_ack_plus_absence_from_active_confirms(self):
        ad, recorded, _ = build(active_by_call=[[SID, OTHER], [OTHER]])
        ack = ad.interrupt(SID)
        self.assertTrue(ack.ok)  # {"interrupted": true} is an ACK
        r = ad.confirm_cancel(SID, ack=ack, sleep=lambda s: None)
        self.assertTrue(r.confirmed)
        # every mutating call targeted the exact session only
        for method, path in recorded:
            if method == "POST":
                self.assertIn(f"/api/session/{SID}/", path)
                self.assertNotIn(OTHER, path)

    def test_ack_alone_never_confirms_when_still_active(self):
        ad, _, _ = build(active_by_call=[[SID, OTHER]])  # never leaves
        ack = ad.interrupt(SID)
        r = ad.confirm_cancel(SID, ack=ack, sleep=lambda s: None,
                              deadline_s=0.05)
        self.assertFalse(r.confirmed)
        self.assertIn("CANCEL_NOT_CONFIRMED", r.reason)

    def test_failed_ack_not_confirmed(self):
        ad, _, _ = build(active_by_call=[[OTHER]], interrupt_exit=1)
        ack = ad.interrupt(SID)
        r = ad.confirm_cancel(SID, ack=ack, sleep=lambda s: None)
        self.assertFalse(r.confirmed)
        self.assertIn("ack failed", r.reason)

    def test_interrupt_targets_exact_session_only(self):
        ad, recorded, _ = build(active_by_call=[[SID, OTHER], [OTHER]])
        ack = ad.interrupt(SID)
        ad.confirm_cancel(SID, ack=ack, sleep=lambda s: None)
        posts = [p for m, p in recorded if m == "POST"]
        self.assertEqual(posts, [f"/api/session/{SID}/interrupt"])
        self.assertTrue(ack.path.endswith(f"/api/session/{SID}/interrupt"))

    def test_other_sessions_survive_cancel(self):
        ad, _, _ = build(active_by_call=[[SID, OTHER], [OTHER]])
        ack = ad.interrupt(SID)
        r = ad.confirm_cancel(SID, ack=ack, sleep=lambda s: None)
        self.assertTrue(r.confirmed)
        self.assertIn(OTHER, ad.active_sessions())


class TestCliKillIsNotCancel(unittest.TestCase):
    def test_killed_by_us_verdict_is_unknown_never_cancelled(self):
        # capture layer flagged our own timeout kill
        v = judge_completion([], exit_code=None, eof=False,
                             killed_by_us=True)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)


class TestReconcileStates(unittest.TestCase):
    def test_running(self):
        ad, _, _ = build(active_by_call=[[SID]])
        self.assertEqual(ad.reconcile(SID), "running")

    def test_pending(self):
        ad, _, _ = build(active_by_call=[[]],
                         pending=[{"id": "per_1", "action": "edit",
                                   "resources": ["x"], "save": [],
                                   "sessionID": SID}])
        self.assertEqual(ad.reconcile(SID), "pending")

    def test_idle_but_idle_is_not_completion(self):
        ad, _, _ = build(active_by_call=[[]], session_exists=True)
        self.assertEqual(ad.reconcile(SID), "idle")
        # idle alone must never complete: judge over an empty current
        # stream says EMPTY_OUTPUT, not COMPLETED
        from hina_opencode_worker.events import (VERDICT_EMPTY_OUTPUT,
                                                 judge_completion)
        v = judge_completion([], exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_EMPTY_OUTPUT)

    def test_missing(self):
        ad, _, _ = build(active_by_call=[[]], session_exists=False)
        self.assertEqual(ad.reconcile(SID), "missing")


if __name__ == "__main__":
    unittest.main()
