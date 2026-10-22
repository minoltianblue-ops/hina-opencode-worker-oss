"""T14 — wait/cancel budgets (MCP.md §4).

1. cancel is ACCEPTED within ~1 s while a worker_wait holds the call
   open — the wait holds no state lock, and the UDS server is
   multi-threaded.
2. A slow backend must never extend a call-response budget: worker_wait
   returns inside its own wait_ms even when backend polls burn whole
   slices and reconcile observations hang far past the control budget.
"""
from __future__ import annotations

import threading
import time
import unittest

from _p4_support import P4TestCase, assert_envelope, outcome_completed, rid


class CancelDuringWait(P4TestCase):
    def test_cancel_accepted_within_one_second_while_waiting(self):
        self.backend.steps = [("hang", None)] * 1000 + [
            outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        att = self.sup._job_row(jid)["active_attempt_id"]

        waiter: dict = {}

        def do_wait():
            waiter["env"] = self.call(
                "worker_wait", {"job_id": jid, "wait_ms": 20000})

        t = threading.Thread(target=do_wait, daemon=True)
        t.start()
        time.sleep(0.4)          # wait is in flight on another socket

        c0 = time.monotonic()
        c = self.call("worker_cancel",
                      {"request_id": rid(), "job_id": jid,
                       "expected_attempt_id": att})
        accepted = time.monotonic() - c0
        assert_envelope(self, c, "worker_cancel")
        self.assertTrue(c["data"]["cancel_requested"])
        self.assertLess(accepted, 1.0,
                        "cancel must be accepted within ~1s even while a"
                        " worker_wait is parked (MCP.md §4)")

        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "wait never returned")
        # the wait saw the signature change (state moved) — not its budget
        self.assertFalse(waiter["env"]["data"]["wait_expired"],
                         "cancel during wait must end the wait early")
        self.assertIn(waiter["env"]["data"]["job"]["state"],
                      ("cancel_requested", "cancelled"))
        self.wait_for(jid, ("cancelled",))

    def test_two_waits_and_catalog_concurrency(self):
        # wait holds nothing: a second wait + catalog run concurrently
        self.backend.steps = [("hang", None)] * 1000 + [
            outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        out = []
        threads = [
            threading.Thread(target=lambda: out.append(
                self.call("worker_wait", {"job_id": jid,
                                          "wait_ms": 300})), daemon=True),
            threading.Thread(target=lambda: out.append(
                self.call("worker_catalog", {"refresh": True})),
                daemon=True),
        ]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=8)
        self.assertLess(time.monotonic() - t0, 4.0,
                        "concurrent wait + catalog serialized")
        self.assertEqual(len(out), 2)
        by_tool = {e["tool"]: e for e in out}
        for e in out:
            self.assertNotIn("error", e, str(e)[:200])
        self.assertTrue(by_tool["worker_wait"]["data"]["wait_expired"])
        self.assertIn("engine", by_tool["worker_catalog"]["data"])
        self.finish(jid)


class SlowBackendCannotExtendWait(P4TestCase):
    def test_backend_burning_poll_slices_still_respects_wait_ms(self):
        # each backend.wait call burns the FULL poll slice; wait_ms is
        # the call-response budget and must bound worker_wait regardless
        self.backend.steps = [("hang", None)] * 2000 + [
            outcome_completed()]
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        t0 = time.monotonic()
        w = self.call("worker_wait", {"job_id": jid, "wait_ms": 500})
        dt = time.monotonic() - t0
        assert_envelope(self, w, "worker_wait")
        self.assertTrue(w["data"]["wait_expired"])
        self.assertLess(dt, 2.0,
                        f"slow backend extended worker_wait to {dt:.2f}s")
        self.finish(jid)

    def test_hanging_reconcile_cannot_extend_wait(self):
        self.backend.steps = [("hang", None)] * 2000 + [
            outcome_completed()]
        self.backend.slow_reconcile_s = 15.0   # blows any control budget
        jid = self.call("worker_start",
                        self.start_args(rid()))["data"]["job"]["job_id"]
        self.wait_for(jid, ("running",))
        t0 = time.monotonic()
        w = self.call("worker_wait", {"job_id": jid, "wait_ms": 600})
        dt = time.monotonic() - t0
        self.assertTrue(w["data"]["wait_expired"])
        self.assertLess(dt, 2.5,
                        f"backend reconcile hang extended worker_wait "
                        f"({dt:.2f}s) — §4 violation")
        self.backend.slow_reconcile_s = 0.0
        self.finish(jid)


if __name__ == "__main__":
    unittest.main()
