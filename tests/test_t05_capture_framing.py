"""T05 — partial UTF-8 / JSON line splitting, unknown/malformed, duplicate
IDs, missing usage and exit codes (M1_WORK_PACKAGES.md T05).

Offline only: recorded event-shape assertions use sanitized golden fixtures
(tests/golden/raw/, provenance in golden_manifest.json); process-level
capture is exercised against a SYNTHETIC fake CLI.
"""
import io
import json
import os
import tempfile
import unittest

from _v2_support import (GOLDEN, RAW, SYNTH, golden_fixture_test,
                         read_raw, read_synth, synth_env, write_fake_cli)

from hina_opencode_worker.events import (EventNormalizer, KIND_MESSAGE,
                                         KIND_UNKNOWN, KIND_USAGE)
from hina_opencode_worker.opencode_v2 import (ContractMismatch,
                                              IncrementalLineDecoder,
                                              MAX_LINE_BYTES,
                                              build_run_argv, capture_run,
                                              parse_jsonl_line, probe)


def normalize_all(raw: bytes, attempt: str = "t05") -> EventNormalizer:
    norm = EventNormalizer(attempt)
    for line in raw.splitlines():
        if line.strip():
            norm.normalize(parse_jsonl_line(line), "T05")
    return norm


class TestManifest(unittest.TestCase):
    def test_fixture_hashes_match_manifest(self):
        golden_fixture_test(self)


class TestIncrementalDecoder(unittest.TestCase):
    def test_multibyte_split_across_chunks_is_safe(self):
        # "한글" is 6 UTF-8 bytes; split mid-character across feed() calls
        line = "한글 텍스트\n".encode("utf-8")
        dec = IncrementalLineDecoder()
        out = []
        for i in range(0, len(line), 2):  # 2-byte chunks cut multibyte chars
            out += dec.feed(line[i:i + 2])
        out += dec.finish()
        self.assertEqual(out, [line[:-1]])
        self.assertEqual(IncrementalLineDecoder.decode(out[0]),
                         "한글 텍스트")

    def test_newline_split_across_chunks(self):
        # Incremental framing emits a line the moment its newline
        # arrives — a completed line is never held back waiting for the
        # rest of the chunk. Only the second line's terminator lands in
        # the next chunk here.
        dec = IncrementalLineDecoder()
        self.assertEqual(dec.feed(b'{"a": 1}\n{"b": 2'), [b'{"a": 1}'])
        self.assertEqual(dec.feed(b"\n"), [b'{"b": 2'])
        self.assertEqual(dec.finish(), [])

    def test_trailing_partial_line_at_eof_is_framed_not_dropped(self):
        dec = IncrementalLineDecoder()
        self.assertEqual(dec.feed(b'{"a": 1}'), [])
        self.assertEqual(dec.finish(), [b'{"a": 1}'])

    def test_line_over_1mib_raises_contract_mismatch(self):
        dec = IncrementalLineDecoder()
        with self.assertRaises(ContractMismatch) as cm:
            dec.feed(b"x" * (MAX_LINE_BYTES + 10))
        self.assertEqual(cm.exception.detail["framing_status"],
                         "CONTRACT_MISMATCH")

    def test_boundary_exactly_1mib_is_allowed(self):
        dec = IncrementalLineDecoder()
        out = dec.feed(b"x" * MAX_LINE_BYTES + b"\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), MAX_LINE_BYTES)

    def test_malformed_utf8_raises_contract_mismatch(self):
        with self.assertRaises(ContractMismatch):
            IncrementalLineDecoder.decode(b'{"t": "\xff\xfe"}')

    def test_non_json_line_preserved_as_sentinel(self):
        # M0-04 malformed band: valid UTF-8, not JSON -> preserved
        env = parse_jsonl_line(b"{not json at all")
        self.assertTrue(env["parse_error"])
        self.assertEqual(env["_raw_line"], "{not json at all")

    def test_non_dict_json_preserved_as_unknown(self):
        norm = EventNormalizer("t")
        for value in (b"[1,2]", b'"str"', b"5", b"null"):
            ev = norm.normalize(parse_jsonl_line(value), "T")
            self.assertEqual(ev.kind, KIND_UNKNOWN)

    def test_unknown_top_level_type_preserved_as_unknown(self):
        raw = (b'{"type": "some_future_type", "sessionID": "ses_x", '
               b'"part": {"id": "prt_1", "type": "weird"}}')
        norm = EventNormalizer("t")
        ev = norm.normalize(parse_jsonl_line(raw), "T")
        self.assertEqual(ev.kind, KIND_UNKNOWN)
        self.assertEqual(ev.payload["unmapped_type"], "some_future_type")


class TestGoldenRawEnvelopes(unittest.TestCase):
    """Sanitized v2.0.30-derived streams: framing, identity, missing usage/exit."""

    def test_m0_01_three_lines_no_step_finish_usage_unknown(self):
        norm = normalize_all(read_raw("m0-01-20261018T093748Z-stdout.jsonl"))
        self.assertEqual(norm._seq, 3)
        self.assertEqual(norm.session_id, "ses_d919faf6b3baa950ba875e54ca")
        self.assertEqual(len(norm.turn_ids), 1)

    def test_m0_02_step_finish_midturn_and_uuid_call_ids(self):
        raw = read_raw("m0-02-20261018T093841Z-stdout.jsonl")
        norm = normalize_all(raw)
        self.assertEqual(norm._seq, 19)
        self.assertEqual(len(norm.turn_ids), 4)
        kinds = [e.kind for e in
                 (norm.normalize(parse_jsonl_line(l), "T")
                  for l in raw.splitlines() if l.strip())]
        self.assertEqual(kinds[-1], KIND_MESSAGE)  # NORMAL-END: text last
        self.assertNotEqual(kinds[-1], KIND_USAGE)

    def test_m0_03_both_call_id_variants(self):
        raw = read_raw("m0-03-20261018T094523Z-stdout.jsonl")
        norm = normalize_all(raw)
        call_ids = [e.payload.get("call_id")
                    for e in (norm.normalize(parse_jsonl_line(l), "T")
                              for l in raw.splitlines() if l.strip())
                    if e.kind == "tool_finished"]
        self.assertTrue(any(c.startswith("chatcmpl-tool-") for c in call_ids))
        self.assertTrue(any(len(c) == 36 and "-" in c for c in call_ids),
                        "UUID variant call IDs must also be handled")

    def test_missing_exit_never_fabricated(self):
        raw = read_raw("m0-03-20261018T094523Z-stdout.jsonl")
        norm = normalize_all(raw)
        exits = [e.payload.get("exit")
                 for e in (norm.normalize(parse_jsonl_line(l), "T")
                           for l in raw.splitlines() if l.strip())
                 if e.kind == "tool_finished"]
        self.assertIn(None, exits)  # read/edit tools have no exit
        self.assertTrue(all(x is None or isinstance(x, int) for x in exits))

    def test_normalized_projection_matches_golden(self):
        for raw_name, gold_name in (
                ("m0-01-20261018T093748Z-stdout.jsonl", "m0-01-normalized.json"),
                ("m0-02-20261018T093841Z-stdout.jsonl", "m0-02-normalized.json"),
                ("m0-03-20261018T094523Z-stdout.jsonl", "m0-03-normalized.json")):
            doc = json.loads((GOLDEN / "normalized" / gold_name).read_text())
            norm2 = EventNormalizer("t05")
            got = []
            for line in read_raw(raw_name).splitlines():
                if not line.strip():
                    continue
                ev = norm2.normalize(parse_jsonl_line(line), "GOLDEN")
                got.append({"seq": ev.seq, "kind": ev.kind,
                            "source_kind": ev.source_kind,
                            "source_event_id": ev.source_event_id,
                            "turn_id": ev.turn_id,
                            "session_id": ev.session_id})
            self.assertEqual(got, doc["events"], gold_name)
            self.assertEqual(norm2.turn_ids, doc["turn_ids"], gold_name)


class TestDuplicateIds(unittest.TestCase):
    def test_identical_payload_deduped_different_payload_never_merged(self):
        norm = EventNormalizer("t")
        events = []
        for line in read_synth("dup-id.jsonl").splitlines():
            env = parse_jsonl_line(line)
            dup = norm.check_dup(env)
            if dup is not None:
                norm.note_duplicate(dup)
                if dup.duplicate:
                    continue
            ev = norm.normalize(env, "T")
            if dup is not None and dup.conflict:
                ev.payload["dup_conflict"] = True
            events.append(ev)
        self.assertEqual(norm.duplicates, 1)       # identical payload dropped
        self.assertEqual(norm.dup_conflicts, 1)    # different payload kept
        self.assertEqual(len(events), 2)           # first + conflicting one
        self.assertTrue(events[1].payload["dup_conflict"])
        self.assertNotEqual(events[0].payload["text"],
                            events[1].payload["text"])

    def test_no_source_id_never_merged_by_content(self):
        norm = EventNormalizer("t")
        kept = 0
        for line in read_synth("no-source-id.jsonl").splitlines():
            env = parse_jsonl_line(line)
            self.assertIsNone(norm.check_dup(env))  # no ID -> no dedupe key
            norm.normalize(env, "T")
            kept += 1
        self.assertEqual(kept, 2)


class TestCaptureRun(unittest.TestCase):
    """Process-level capture against the SYNTHETIC fake CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.binary = write_fake_cli(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _capture(self, raw: bytes, exit_code: int = 0, **extra):
        env = synth_env(os.environ.copy(), self.tmp.name, raw, exit_code,
                        **extra)
        out, err = io.BytesIO(), io.BytesIO()
        res = capture_run(self.binary, model="prov/model-x", title="t05",
                          prompt="p", cwd=self.tmp.name, stdout_sink=out,
                          stderr_sink=err, env=env, timeout_s=30.0)
        return res, out.getvalue(), err.getvalue()

    def test_golden_bytes_round_trip_and_hash(self):
        raw = read_raw("m0-02-20261018T093841Z-stdout.jsonl")
        res, out_bytes, _ = self._capture(raw)
        self.assertEqual(res.exit_code, 0)
        self.assertTrue(res.eof)
        self.assertFalse(res.contract_mismatch)
        self.assertEqual(res.stdout_bytes, len(raw))
        import hashlib
        self.assertEqual(res.stdout_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(out_bytes, raw)
        self.assertEqual(res.framing["lines"], 19)

    def test_chunked_writes_produce_same_events(self):
        raw = read_raw("m0-03-20261018T094523Z-stdout.jsonl")
        # Keep all 2,650 seven-byte writes and all golden bytes. Only the
        # first eight writes need an artificial delay to exercise streaming;
        # thousands of timer waits can take minutes on a throttled remote host.
        res, out_bytes, _ = self._capture(raw, HINA_FAKE_CHUNK_OUT=7,
                                          HINA_FAKE_DELAY_OUT=0.005,
                                          HINA_FAKE_DELAY_CHUNKS=8)
        self.assertEqual(res.exit_code, 0)
        self.assertTrue(res.eof)
        self.assertEqual(out_bytes, raw)
        self.assertEqual(res.framing["lines"], 26)

    def test_huge_line_contract_mismatch_never_drops_raw(self):
        huge = b'{"a": "' + b"x" * (MAX_LINE_BYTES + 4096) + b'"}\n'
        res, out_bytes, _ = self._capture(huge)
        self.assertTrue(res.contract_mismatch)
        self.assertEqual(res.framing["framing_status"], "CONTRACT_MISMATCH")
        self.assertEqual(out_bytes, huge)  # raw preserved, never dropped

    def test_empty_output_exit0_not_success(self):
        res, out_bytes, _ = self._capture(b"")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.stdout_bytes, 0)

    def test_nonzero_exit_recorded(self):
        res, _, err_bytes = self._capture(
            read_raw("fake-band-exit3.jsonl"), exit_code=3)
        self.assertEqual(res.exit_code, 3)

    def test_timeout_kills_only_own_child_never_cancelled(self):
        env = synth_env(os.environ.copy(), self.tmp.name, b"", 0,
                        HINA_FAKE_SLEEP=5)
        out, err = io.BytesIO(), io.BytesIO()
        res = capture_run(self.binary, model="prov/model-x", title="t05",
                          prompt="p", cwd=self.tmp.name, stdout_sink=out,
                          stderr_sink=err, env=env, timeout_s=0.5)
        self.assertTrue(res.killed_by_us)
        self.assertFalse(res.eof)
        self.assertNotIn("cancel", str(res.mismatch_reason or "").lower())

    def test_malformed_utf8_line_contract_mismatch(self):
        raw = b'{"type": "text", "part": {"text": "\xff\xfe"}}\n'
        res, out_bytes, _ = self._capture(raw)
        self.assertTrue(res.contract_mismatch)
        self.assertEqual(out_bytes, raw)


class TestProbeAndArgv(unittest.TestCase):
    def test_version_mismatch_blocks(self):
        with self.assertRaises(ContractMismatch):
            probe("/nonexistent", version_out="opencode v9.9.9",
                  sha256_hex="d" * 64)

    def test_fingerprint_mismatch_blocks(self):
        with self.assertRaises(ContractMismatch):
            probe("/nonexistent", version_out="opencode v2.0.30",
                  sha256_hex="0" * 64)

    def test_pinned_pair_passes(self):
        from hina_opencode_worker.opencode_v2 import (MAPPING_REVISION,
                                                      PINNED_BINARY_SHA256)
        r = probe("/nonexistent", version_out="opencode v2.0.30",
                  sha256_hex=PINNED_BINARY_SHA256)
        self.assertEqual(r.mapping_revision, MAPPING_REVISION)

    def test_model_required_never_defaulted(self):
        with self.assertRaises(ValueError):
            build_run_argv("bin", model="", title="t", prompt="p")

    def test_argv_shape_no_session_flag(self):
        argv = build_run_argv("bin", model="prov/m", title="t", prompt="p")
        self.assertEqual(argv, ["bin", "run", "--model", "prov/m",
                                "--format", "json", "--title", "t", "p"])
        self.assertNotIn("--session", argv)  # continue/create is OPEN_LIVE


if __name__ == "__main__":
    unittest.main()
