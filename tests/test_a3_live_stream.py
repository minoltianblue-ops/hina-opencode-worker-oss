#!/usr/bin/env python3
"""T-A3-3 — M4-A3 live-backend fixes (offline, fake CLI only):

Bug 1a: capture_and_normalize swallowed the caller's on_line into
**kw and passed it a second time -> "capture_run() got multiple values
for keyword argument 'on_line'" killed every LiveBackend job (first
ChatGPT round trip, job_5e4329c9…).
Bug 1b: LiveBackend.wait ran capture_run(timeout=slice) per wait slice
(WAIT_POLL_S=0.1) — the first slice would kill our own child and
misjudge every job. The fix streams ONE long-lived child per attempt
across slices (_LiveStream); cancel terminates ONLY our owned child.

All offline: the fake CLI (tests/_v2_support) emits the golden
M0-01 NORMAL-END fixture; no opencode binary, no server, no model.
"""
from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
from pathlib import Path

from _v2_support import read_raw, write_fake_cli

from hina_opencode_worker.events import VERDICT_COMPLETED
from hina_opencode_worker.opencode_v2 import capture_and_normalize
from hina_opencode_worker.supervisor import BackendRun, LiveBackend

GOLDEN_NORMAL_END = "m0-01-20261018T093748Z-stdout.jsonl"


class _FakeCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cli = write_fake_cli(self.tmp.name)
        self._saved = {}

    def env_script(self, **vars_):
        for k, v in vars_.items():
            self._saved.setdefault(k, os.environ.get(k))
            os.environ[k] = str(v)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestCaptureAndNormalizeOnLine(_FakeCliCase):
    def test_caller_hook_no_keyword_collision(self):
        raw = read_raw(GOLDEN_NORMAL_END)
        script = Path(self.tmp.name) / "stdout.jsonl"
        script.write_bytes(raw)
        self.env_script(HINA_FAKE_STDOUT=str(script), HINA_FAKE_EXIT=0)
        lines = []
        res, _norm, events = capture_and_normalize(
            self.cli, attempt_id="att_hook", model="fake/model",
            title="t", prompt="p", cwd=self.tmp.name,
            stdout_sink=io.BytesIO(), stderr_sink=io.BytesIO(),
            on_line=lines.append, timeout_s=30)
        self.assertEqual(res.exit_code, 0)
        self.assertTrue(events)
        self.assertEqual(len(lines),
                         len([l for l in raw.splitlines() if l.strip()]))


class TestLiveSliceStream(_FakeCliCase):
    def _backend_and_run(self):
        b = LiveBackend(binary=self.cli,
                        server_url="http://127.0.0.1:1", adapter=None)
        run = BackendRun("ses_test_slice")
        run.handle = (self.tmp.name, "fake/model", "prompt", "att_slice")
        return b, run

    def test_one_child_streamed_across_slices(self):
        raw = read_raw(GOLDEN_NORMAL_END)
        script = Path(self.tmp.name) / "stdout.jsonl"
        script.write_bytes(raw)
        # emit fully, stay alive ~1s, exit 0 -> MANY 0.05s slices pass
        # while the child runs; the child is NEVER killed or re-spawned
        self.env_script(HINA_FAKE_STDOUT=str(script), HINA_FAKE_EXIT=0,
                        HINA_FAKE_SLEEP="1.0")
        b, run = self._backend_and_run()
        cancel = threading.Event()
        outcome, slices = None, 0
        while outcome is None and slices < 200:
            outcome = b.wait(run, cancel=cancel, deadline_s=0.05,
                             on_permission=None)
            slices += 1
        self.assertIsNotNone(outcome)
        self.assertGreater(slices, 3, "must span multiple wait slices")
        self.assertEqual(outcome.verdict, VERDICT_COMPLETED)
        self.assertEqual(outcome.raw_stdout, raw)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.session_id, "ses_test_slice")
        # one child only: the stream registry is drained on completion
        self.assertEqual(b._streams, {})

    def test_cancel_terminates_only_own_child(self):
        script = Path(self.tmp.name) / "stdout.jsonl"
        script.write_bytes(b"")
        self.env_script(HINA_FAKE_STDOUT=str(script), HINA_FAKE_EXIT=0,
                        HINA_FAKE_SLEEP="30")
        b, run = self._backend_and_run()
        cancel = threading.Event()
        self.assertIsNone(b.wait(run, cancel=cancel, deadline_s=0.05,
                                 on_permission=None))
        st = b._streams["ses_test_slice"]
        pid = st.proc.pid
        cancel.set()
        self.assertIsNone(b.wait(run, cancel=cancel, deadline_s=0.05,
                                 on_permission=None))
        self.assertEqual(b._streams, {})
        self.assertIsNotNone(st.proc.poll(),
                             "our owned child must be terminated")
        self.assertTrue(pid)


if __name__ == "__main__":
    unittest.main()
