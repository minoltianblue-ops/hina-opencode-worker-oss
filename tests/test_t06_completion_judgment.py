"""T06 — CLI exit 0 + failed tool must not read as success; a PREVIOUS
turn's final answer/idle must never satisfy the current turn
(M1_WORK_PACKAGES.md T06).

Grounded in the OBSERVED NORMAL-END row (EOF + exit 0 + trailing text;
step_finish never terminal) and the M0-04 fake bands (SYNTHETIC).
"""
import unittest

from _v2_support import read_raw, read_synth

from hina_opencode_worker.events import (EventNormalizer,
                                         VERDICT_CLI_FAILED,
                                         VERDICT_CLI_OK_TOOL_FAILED,
                                         VERDICT_COMPLETED,
                                         VERDICT_CONTRACT_MISMATCH,
                                         VERDICT_EMPTY_OUTPUT,
                                         VERDICT_NO_FINAL_ANSWER,
                                         VERDICT_UNKNOWN, judge_completion)
from hina_opencode_worker.opencode_v2 import parse_jsonl_line


def stream(raw: bytes, attempt: str = "t06"):
    norm = EventNormalizer(attempt)
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        env = parse_jsonl_line(line)
        dup = norm.check_dup(env) if isinstance(env, dict) \
            and not env.get("parse_error") else None
        if dup is not None:
            norm.note_duplicate(dup)
            if dup.duplicate:
                continue
        events.append(norm.normalize(env, "T06"))
    return norm, events


class TestRealGoldenCompletion(unittest.TestCase):
    def test_m0_01_completed_with_usage_unknown(self):
        # real run with NO step_finish at all — still a normal end
        _, events = stream(read_raw("m0-01-20261018T093748Z-stdout.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_COMPLETED)
        self.assertEqual(v.usage, "unknown")  # missing usage, never 0

    def test_m0_02_completed_usage_from_last_step_finish(self):
        norm, events = stream(read_raw("m0-02-20261018T093841Z-stdout.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True,
                             current_turn_ids=set(norm.turn_ids))
        self.assertEqual(v.verdict, VERDICT_COMPLETED)
        self.assertIsInstance(v.usage, dict)
        self.assertEqual(v.usage["reason"], "tool-calls")
        self.assertIn("tokens", v.usage)

    def test_m0_03_ends_on_text_not_step_finish(self):
        norm, events = stream(read_raw("m0-03-20261018T094523Z-stdout.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True,
                             current_turn_ids=set(norm.turn_ids))
        self.assertEqual(v.verdict, VERDICT_COMPLETED)


class TestFakeBands(unittest.TestCase):
    """SYNTHETIC M0-04 bands: each failure axis stays distinct."""

    def test_exit3_cli_failed(self):
        _, events = stream(read_raw("fake-band-exit3.jsonl"))
        v = judge_completion(events, exit_code=3, eof=True)
        self.assertEqual(v.verdict, VERDICT_CLI_FAILED)

    def test_empty_exit0_never_success(self):
        v = judge_completion([], exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_EMPTY_OUTPUT)

    def test_malformed_line_preserved_not_success(self):
        _, events = stream(read_raw("fake-band-malformed.jsonl"))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].payload.get("parse_error"))
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)

    def test_error_event_preserved_never_completed(self):
        _, events = stream(read_raw("fake-band-error.jsonl"))
        errs = [e for e in events if e.kind == "provider_error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].payload["error_name"], "ProviderAuthError")
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)  # classification is the caller's

    def test_nofinish_completed_usage_unknown(self):
        _, events = stream(read_raw("fake-band-nofinish.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_COMPLETED)
        self.assertEqual(v.usage, "unknown")

    def test_checkfail_stream_ends_on_step_finish_not_completed(self):
        # M0-04 checkfail band: exit 0, well-formed, but the harness CHECK
        # failed. Our verdict axis is separate from verification: the
        # stream ends on step_finish (reason stop) which is NOT terminal,
        # and no judge verdict may ever claim verification success.
        _, events = stream(read_raw("fake-band-checkfail.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_NO_FINAL_ANSWER)


class TestToolFailureWithExit0(unittest.TestCase):
    def test_shell_exit1_status_completed_is_tool_failed(self):
        _, events = stream(read_synth("tool-fail-exit0.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_CLI_OK_TOOL_FAILED)
        self.assertEqual(len(v.failed_tools), 1)
        self.assertEqual(v.failed_tools[0]["exit"], 1)
        self.assertEqual(v.failed_tools[0]["call_id"],
                         "11111111-2222-3333-4444-555555555555")

    def test_tool_status_error_is_tool_failed(self):
        raw = (b'{"type": "tool_use", "sessionID": "ses_x", "part": '
               b'{"id": "c1", "partID": "p1", "type": "tool", "tool": "shell", '
               b'"messageID": "msg_1", "state": {"status": "error"}}}')
        _, events = stream(raw)
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_CLI_OK_TOOL_FAILED)

    def test_missing_exit_not_treated_as_zero_failure(self):
        # read tool: no exit at all, status completed -> not a failure
        raw = (b'{"type": "tool_use", "sessionID": "ses_x", "part": '
               b'{"id": "c1", "partID": "p1", "type": "tool", "tool": "read", '
               b'"messageID": "msg_1", "state": {"status": "completed"}}}\n'
               b'{"type": "text", "sessionID": "ses_x", "part": {"id": "p2", '
               b'"type": "text", "messageID": "msg_1", "text": "ok"}}')
        _, events = stream(raw)
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_COMPLETED)


class TestTurnConfusionGuard(unittest.TestCase):
    """The T06 trap: previous turn's final/idle must not complete the
    current turn (M0-08 adopted TURN-ID row)."""

    def test_prev_turn_final_never_satisfies_current_turn(self):
        norm, events = stream(
            read_synth("prev-turn-final-then-current-open.jsonl"))
        curr = {"msg_FAKE_curr"}
        v = judge_completion(events, exit_code=0, eof=True,
                             current_turn_ids=curr)
        self.assertNotEqual(v.verdict, VERDICT_COMPLETED)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)
        self.assertEqual(v.prev_turn_events, 2)
        # without the filter the trap WOULD misjudge — proving the guard
        # is what prevents it, not luck of ordering
        trap = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(trap.verdict, VERDICT_COMPLETED)

    def test_current_turn_open_on_step_finish_not_completed(self):
        _, events = stream(read_synth("ends-on-step-finish.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True)
        self.assertEqual(v.verdict, VERDICT_NO_FINAL_ANSWER)

    def test_no_events_for_current_turn_is_empty_not_idle_success(self):
        _, events = stream(
            read_synth("prev-turn-final-then-current-open.jsonl"))
        v = judge_completion(events, exit_code=0, eof=True,
                             current_turn_ids={"msg_FAKE_unstarted"})
        self.assertEqual(v.verdict, VERDICT_EMPTY_OUTPUT)


class TestEndConditions(unittest.TestCase):
    def setUp(self):
        self.norm, self.events = stream(
            read_raw("m0-01-20261018T093748Z-stdout.jsonl"))

    def test_missing_exit_code_is_unknown_not_zero(self):
        v = judge_completion(self.events, exit_code=None, eof=True)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)

    def test_killed_by_us_never_cancelled(self):
        v = judge_completion(self.events, exit_code=-9, eof=False,
                             killed_by_us=True)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)
        self.assertNotEqual(v.verdict, "CANCELLED")

    def test_contract_mismatch_wins_over_exit0(self):
        v = judge_completion(self.events, exit_code=0, eof=True,
                             contract_mismatch=True)
        self.assertEqual(v.verdict, VERDICT_CONTRACT_MISMATCH)

    def test_trailing_text_without_eof_not_completed(self):
        v = judge_completion(self.events, exit_code=0, eof=False)
        self.assertEqual(v.verdict, VERDICT_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
