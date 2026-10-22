"""M4-A3 ChatGPT-delivery contract regression (offline, fake CLI only).

Round-trip 2026-10-20 reached the backend and answered locally
(CODE=HINA-A3-92483418) but ChatGPT-side delivery was blocked:
  defect 1 — artifact ids were emitted art-<sha256-hex> ("art-eaa043a4…"
             rejected); the frozen contract (MCP.md §2, mcp-tools.json
             patterns) requires art_<lowercase UUIDv4>.
  defect 2 — the summary text carried only goal+verdict lines; the ANSWER
             never appeared, so the connector had nothing to deliver.

Tests: id format at every emission site and in the connector-facing
list/content views validated against the frozen outputSchema; summary
contains the answer verbatim (private row + masked export + connector
item); _final_answer_text extraction incl. streamed-snapshot dedupe;
LiveBackend.finalize wiring on the M0-01 golden fixture (no model, no
server, no network).
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from _p4_support import (P4TestCase, assert_envelope, fake_att_id,
                         outcome_completed, rid)
from _v2_support import read_raw, write_fake_cli

from hina_opencode_worker import artifacts as art
from hina_opencode_worker.events import EventNormalizer, VERDICT_COMPLETED
from hina_opencode_worker.supervisor import (AttemptOutcome, BackendRun,
                                             LiveBackend, Supervisor,
                                             _ART_ID_RE, _final_answer_text,
                                             validate_result)

# The exact rejection observed from the ChatGPT side (defect 1).
LEGACY_REJECTED_ID = "art-eaa043a44cc43d25a929ffb5"
UUID4_RE = re.compile(
    r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")
ANSWER = "CODE is HINA-A3-92483418 — the requested token."
GOLDEN_NORMAL_END = "m0-01-20261018T093748Z-stdout.jsonl"


def _attempt_store(root):
    return art.ArtifactStore(Path(root) / "artifacts")


class TestArtifactIdFormat(unittest.TestCase):
    """Every artifact row the connector can ever see carries art_<UUIDv4>."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _attempt_store(self.tmp.name)
        self.att = fake_att_id()

    def _all_emission_sites(self):
        rows = []
        priv = self.store.save_private(
            job_id="job_" + "0" * 36, attempt_id=self.att,
            result_revision=1, section="raw", relative_path="stdout.raw",
            data=b'{"type":"text","part":{"text":"hi"}}\n',
            media_type="application/x-ndjson")
        rows.append(self.store.sink.get(priv.artifact_id))
        sess = self.store.export_masked(
            private=priv, job_id="job_" + "0" * 36, attempt_id=self.att,
            result_revision=1, relative_path="content/stdout.raw")
        rows.append(self.store.sink.get(sess.record.artifact_id))
        page = self.store.save_export_page(
            private=priv, job_id="job_" + "0" * 36, attempt_id=self.att,
            result_revision=1, relative_path="export/page-0",
            page_bytes=b"page", complete=False)
        rows.append(self.store.sink.get(page.artifact_id))
        return rows

    def test_every_emission_matches_uuidv4(self):
        for row in self._all_emission_sites():
            aid = row["artifact_id"]
            self.assertRegex(aid, UUID4_RE,
                             f"connector-facing id must be art_<UUIDv4>: {aid}")
            self.assertRegex(aid, _ART_ID_RE)  # tool-input validator agrees
            self.assertRegex(aid, art.ARTIFACT_ID_RE)

    def test_ids_unique_across_many_rows(self):
        seen = set()
        for i in range(200):
            rec = self.store.save_private(
                job_id="job_" + "0" * 36, attempt_id=self.att,
                result_revision=1, section="raw",
                relative_path=f"unique/{i}.txt", data=f"x{i}".encode(),
                media_type="text/plain")
            self.assertNotIn(rec.artifact_id, seen)
            seen.add(rec.artifact_id)

    def test_legacy_hex_derived_id_is_rejected_at_the_tool_gate(self):
        # The shape ChatGPT was handed and refused; our own boundary must
        # refuse it too (MCP.md §2: art_ + lowercase UUIDv4).
        from hina_opencode_worker.supervisor import WorkerError
        with self.assertRaises(WorkerError) as cm:
            validate_result({"job_id": "job_" + str(__import__("uuid").uuid4()),
                             "section": "content",
                             "artifact_id": LEGACY_REJECTED_ID})
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")

    def test_uuidv4_id_passes_the_tool_gate(self):
        import uuid
        aid = "art_" + str(uuid.uuid4())
        out = validate_result(
            {"job_id": "job_" + str(uuid.uuid4()), "section": "content",
             "artifact_id": aid, "limit": 5, "byte_limit": 1024})
        self.assertEqual(out["artifact_id"], aid)


def _msg_events(texts, *, turn="msg_1", part_prefix="prt_",
                final_type="text"):
    norm = EventNormalizer("att_fmt")
    events = []
    for i, t in enumerate(texts):
        if isinstance(t, tuple):  # non-message tail: (type, part)
            events.append(norm.normalize({"type": t[0], "part": t[1]}, "T"))
            continue
        events.append(norm.normalize(
            {"type": final_type,
             "part": {"id": f"{part_prefix}{i}", "messageID": turn,
                      "text": t}}, "T"))
    return events


class TestFinalAnswerExtraction(unittest.TestCase):
    """_final_answer_text mirrors judge_completion's trailing-message
    rule and returns the answer VERBATIM (or None; never fabricated)."""

    def test_single_trailing_message_is_verbatim(self):
        evs = _msg_events([("step_start", {"id": "prt_s"}), ANSWER])
        self.assertEqual(_final_answer_text(evs), ANSWER)

    def test_interleaved_tool_excludes_earlier_parts(self):
        # only the TRAILING message run is the answer — text before an
        # intervening tool part is not part of it
        evs = _msg_events(["before tool",
                           ("tool_use", {"tool": "bash", "state":
                                         {"status": "completed"}}),
                           ANSWER])
        self.assertEqual(_final_answer_text(evs), ANSWER)

    def test_streamed_snapshots_collapse_to_latest_per_part(self):
        # same part id, growing text (dup_conflict keeps every snapshot):
        # the LATEST snapshot only, once.
        norm = EventNormalizer("att_fmt")
        evs = []
        for grow in ("CODE", "CODE is HINA-", ANSWER):
            evs.append(norm.normalize(
                {"type": "text", "part": {"id": "prt_a", "messageID": "msg_1",
                                          "text": grow}}, "T"))
        self.assertEqual(_final_answer_text(evs), ANSWER)

    def test_distinct_trailing_parts_join_in_order_without_filler(self):
        evs = _msg_events(["part one", " part two"])
        self.assertEqual(_final_answer_text(evs), "part one part two")

    def test_empty_final_snapshot_keeps_the_real_answer(self):
        evs = _msg_events([ANSWER, ""])
        self.assertEqual(_final_answer_text(evs), ANSWER)

    def test_no_trailing_message_returns_none(self):
        evs = _msg_events([ANSWER, ("step_finish", {"reason": "stop"})])
        self.assertIsNone(_final_answer_text(evs))

    def test_empty_stream_returns_none(self):
        self.assertIsNone(_final_answer_text([]))
        self.assertIsNone(_final_answer_text(None))


class TestLiveBackendAnswerWiring(unittest.TestCase):
    """Golden M0-01 NORMAL-END through the real LiveBackend wait path:
    finalize() must surface the trailing text as outcome.answer_text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cli = write_fake_cli(self.tmp.name)
        self.raw = read_raw(GOLDEN_NORMAL_END)
        script = Path(self.tmp.name) / "stdout.jsonl"
        script.write_bytes(self.raw)
        import os
        os.environ["HINA_FAKE_STDOUT"] = str(script)
        os.environ["HINA_FAKE_EXIT"] = "0"
        self.addCleanup(os.environ.pop, "HINA_FAKE_STDOUT", None)
        self.addCleanup(os.environ.pop, "HINA_FAKE_EXIT", None)

    def test_finalize_exposes_golden_answer_text(self):
        expected = None
        for line in self.raw.splitlines():
            if line.strip():
                env = json.loads(line)
                if env.get("type") == "text":
                    expected = (env.get("part") or {}).get("text")
        self.assertEqual(expected, "HINA_M0_TEXT_OK")  # fixture sanity

        b = LiveBackend(binary=self.cli, server_url="http://127.0.0.1:1",
                        adapter=None)
        run = BackendRun("ses_test_answer")
        run.handle = (self.tmp.name, "fake/model", "prompt", "att_answer")
        cancel = threading.Event()
        outcome, guard = None, 0
        while outcome is None and guard < 200:
            outcome = b.wait(run, cancel=cancel, deadline_s=0.05,
                             on_permission=None)
            guard += 1
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.verdict, VERDICT_COMPLETED)
        self.assertEqual(outcome.answer_text, expected,
                         "answer must reach the outcome verbatim")


class TestSummaryCarriesAnswer(P4TestCase):
    """Connector-facing contract: worker_result summary contains the
    answer text verbatim, and the artifacts listed to the connector use
    art_<UUIDv4> through the full list→content path."""

    def _completed_with_answer(self):
        self.backend.steps = [AttemptOutcome(
            VERDICT_COMPLETED, "EOF + exit 0 + final message",
            session_id="ses_fake0001", raw_stdout=b'{"type":"text"}\n',
            exit_code=0, answer_text=ANSWER)]
        env = self.call("worker_start", self.start_args())
        self.assertIn("data", env, f"start failed: {env}")
        jid = env["data"]["job"]["job_id"]
        self.wait_for(jid, ("completed",))
        return jid

    def test_summary_section_contains_answer_verbatim(self):
        jid = self._completed_with_answer()
        res = self.call("worker_result", {"job_id": jid})
        assert_envelope(self, res, "worker_result")  # frozen outputSchema
        item = res["data"]["items"][0]
        self.assertEqual(item["kind"], "summary")
        self.assertIn(ANSWER, item["text"],
                      "connector must see the answer in the summary")
        self.assertIn(f"answer:\n{ANSWER}", item["text"])
        self.assertIn("result: ", item["text"])  # verdict line preserved

    def test_summary_without_answer_stays_unfabricated(self):
        # legacy fake outcome (no answer_text): no empty answer block
        jid = self.completed_start()
        res = self.call("worker_result", {"job_id": jid})
        assert_envelope(self, res, "worker_result")
        self.assertNotIn("answer:", res["data"]["items"][0]["text"])

    def test_connector_list_and_content_round_trip(self):
        jid = self._completed_with_answer()
        lst = self.call("worker_result",
                        {"job_id": jid, "section": "artifacts", "limit": 50})
        assert_envelope(self, lst, "worker_result")  # schema pins id pattern
        self.assertTrue(lst["data"]["items"], "artifacts must be listed")
        for it in lst["data"]["items"]:
            self.assertRegex(it["artifact_id"], UUID4_RE)
            self.assertNotIn(LEGACY_REJECTED_ID, it["artifact_id"])

        # the summary export row specifically: id -> content by id
        conn = self.sup._read()
        try:
            row = conn.execute(
                "SELECT artifact_id FROM artifacts WHERE job_id = ?"
                " AND visibility = 'export' AND relative_path ="
                " 'export/summary/summary.txt'", (jid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "summary export row missing")
        self.assertRegex(row["artifact_id"], UUID4_RE)

        got, cur = b"", None
        while True:
            args = {"job_id": jid, "section": "content",
                    "artifact_id": row["artifact_id"], "byte_limit": 4096}
            if cur:
                args["cursor"] = cur
            res = self.call("worker_result", args)
            assert_envelope(self, res, "worker_result")
            chunk = res["data"]["items"][0]
            got += base64.b64decode(chunk["data_base64"])
            if chunk["last"]:
                break
            cur = res["data"]["next_cursor"]
            self.assertIsNotNone(cur)
        self.assertIn(ANSWER.encode("utf-8"), got,
                      "masked export must preserve the answer verbatim "
                      "(no secret patterns in this answer)")

    def test_private_summary_row_contains_answer_before_masking(self):
        jid = self._completed_with_answer()
        conn = self.sup._read()
        try:
            rows = conn.execute(
                "SELECT artifact_id, visibility FROM artifacts WHERE"
                " job_id = ? AND relative_path IN ('src/summary/summary.txt',"
                " 'export/summary/summary.txt')", (jid,)).fetchall()
        finally:
            conn.close()
        self.assertEqual({r["visibility"] for r in rows},
                         {"private", "export"})
        store = self.sup._artifact_store
        for r in rows:
            if r["visibility"] == "private":
                data = store.read_private(r["artifact_id"])
                self.assertIn(ANSWER.encode("utf-8"), data)


if __name__ == "__main__":
    unittest.main()
