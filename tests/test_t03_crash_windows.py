"""T03 — prepared/sending/acknowledged 각각 강제 종료; 불명확 dispatch 재실행 0.

(M1_WORK_PACKAGES.md 필수 자동화 테스트 T03)

The child process is SIGKILLed after each committed phase, so durability
is real (WAL + synchronous=FULL), not simulated. After the kill a fresh
Store reconciles per STORAGE.md §3 ④:
- prepared (never dispatched)      -> resumable, dispatch may happen once
- sending / acknowledged           -> ambiguous + needs_attention, and
  re-dispatch is impossible (phase CAS refuses).
needs_attention is never auto-moved to an active state.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from hina_opencode_worker.requests import RequestJournal
from hina_opencode_worker.store import Store

from tests._support import StoreTestCase

CHILD = str(Path(__file__).resolve().parent / "_crash_child.py")


class T03CrashWindows(StoreTestCase):
    def run_child_to(self, up_to: str) -> None:
        marker = self.tmp / "marker"
        dispatch_log = self.tmp / "dispatch.log"
        proc = subprocess.Popen(
            [sys.executable, CHILD, str(self.db), str(marker), str(dispatch_log), up_to],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 30.0
        target = up_to
        while time.monotonic() < deadline:
            if marker.exists():
                marks = marker.read_text(encoding="utf-8").split()
                if target in marks:
                    break
            if proc.poll() is not None:
                self.fail(
                    f"child died early rc={proc.returncode}: "
                    f"{proc.stderr.read().decode(errors='replace')}"
                )
            time.sleep(0.05)
        else:
            proc.kill()
            self.fail(f"child never reached phase {up_to!r}")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
        self.dispatch_log = dispatch_log

    def dispatch_count(self) -> int:
        log = getattr(self, "dispatch_log", None)
        if log is None or not log.exists():
            return 0
        return len(log.read_text(encoding="utf-8").splitlines())

    def reopen(self) -> Store:
        store = Store.open(self.db)
        self.addCleanup(store.close)
        return store

    def test_kill_at_prepared_is_resumable_and_dispatches_once(self):
        self.run_child_to("prepared")
        store = self.reopen()
        plans = store.reconcile_after_restart()
        self.assertEqual(plans, [("req-crash", "resume")])
        row = store.get_request("u1", "req-crash")
        self.assertEqual(row["phase"], "prepared")
        self.assertEqual(store.get_job(row["job_id"])["state"], "accepted")
        self.assertEqual(self.dispatch_count(), 0)

        # Resume: the new supervisor may claim the single dispatch.
        journal = RequestJournal(store)
        self.assertTrue(journal.claim_dispatch("u1", "req-crash"))
        self.assertFalse(journal.claim_dispatch("u1", "req-crash"))
        self.assertEqual(store.get_request("u1", "req-crash")["phase"], "sending")

    def test_kill_at_sending_is_ambiguous_never_redispatched(self):
        self.run_child_to("sending")
        self.assertEqual(self.dispatch_count(), 1)
        store = self.reopen()
        plans = store.reconcile_after_restart()
        self.assertEqual(plans, [("req-crash", "needs_attention")])
        row = store.get_request("u1", "req-crash")
        job = store.get_job(row["job_id"])
        self.assertEqual(row["phase"], "ambiguous")
        self.assertEqual(job["state"], "needs_attention")
        self.assertTrue(job["attention_reason"])
        attempt = store.conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (row["attempt_id"],)
        ).fetchone()
        self.assertEqual(attempt["state"], "needs_attention")

        # Re-dispatch of an unclear dispatch: forbidden (0 further prompts).
        journal = RequestJournal(store)
        self.assertFalse(journal.claim_dispatch("u1", "req-crash"))
        self.assertEqual(self.dispatch_count(), 1)

        # needs_attention is not auto-healed by another restart.
        self.assertEqual(store.reconcile_after_restart(), [])
        self.assertEqual(store.get_job(row["job_id"])["state"], "needs_attention")

    def test_kill_at_acknowledged_is_ambiguous_never_redispatched(self):
        self.run_child_to("acknowledged")
        self.assertEqual(self.dispatch_count(), 1)
        store = self.reopen()
        plans = store.reconcile_after_restart()
        self.assertEqual(plans, [("req-crash", "needs_attention")])
        row = store.get_request("u1", "req-crash")
        self.assertEqual(row["phase"], "ambiguous")
        self.assertEqual(store.get_job(row["job_id"])["state"], "needs_attention")
        journal = RequestJournal(store)
        self.assertFalse(journal.claim_dispatch("u1", "req-crash"))
        self.assertEqual(self.dispatch_count(), 1)

    def test_journal_survives_forced_kill(self):
        self.run_child_to("sending")
        store = self.reopen()
        row = store.get_request("u1", "req-crash")
        events = store.journal(row["job_id"])
        self.assertEqual([e["to_state"] for e in events], ["accepted"])
        self.assertEqual(store.integrity_check(), "ok")

    def test_needs_attention_exit_requires_reconcile_proof(self):
        self.run_child_to("sending")
        store = self.reopen()
        store.reconcile_after_restart()
        row = store.get_request("u1", "req-crash")
        job = store.get_job(row["job_id"])
        with self.assertRaises(Exception) as ctx:
            store.transition_job(job["job_id"], job["version"], "running", "auto-retry")
        self.assertIn("reconcile proof", str(ctx.exception))
        # With proof, an operator-verified exit is legal.
        store.transition_job(
            job["job_id"], job["version"], "running", "reconcile_proven",
            reconcile_proof="backend session/turn observed alive by operator reconcile",
        )
        self.assertEqual(store.get_job(job["job_id"])["state"], "running")


if __name__ == "__main__":
    unittest.main()
