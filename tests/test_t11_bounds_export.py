"""T11 — 32k/500k/1MiB 경계, masking 뒤 export/chunk hash, cursor
mutation/expiry. (M1_WORK_PACKAGES.md T11; WORKSPACE.md ch4–ch5)
"""
from __future__ import annotations

from hina_opencode_worker import artifacts as art
from hina_opencode_worker.artifacts import (
    EXPORT_CHUNK_LIMIT,
    PREVIEW_LIMIT,
    REASON_CURSOR_EXPIRED,
    REASON_CURSOR_MUTATED,
    REASON_CURSOR_STALE,
    REASON_INVALID_ROW,
    REASON_ORDER,
    REASON_REGISTRY_CONFLICT,
    REASON_TOO_LARGE,
    REASON_TRUNCATED,
    ArtifactError,
    ArtifactStore,
    ListRegistrySink,
    SqliteRegistrySink,
)

from tests._wt_support import WtTestCase, new_db


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def store_with(clock=None, sink=None, root="."):
    return ArtifactStore(root, sink=sink or ListRegistrySink(),
                         clock=clock or Clock(), cursor_ttl=900.0,
                         cursor_secret=b"t11-secret")


class TestPreviewBounds(WtTestCase):
    def test_32k_boundary_exact_then_truncated(self):
        exact = b"a" * PREVIEW_LIMIT
        over = b"a" * (PREVIEW_LIMIT + 1)
        p = art.preview_bytes(exact)
        self.assertFalse(p.truncated)
        self.assertEqual(p.shown_bytes, PREVIEW_LIMIT)
        p = art.preview_bytes(over)
        self.assertTrue(p.truncated)          # display-only truncation
        self.assertEqual((p.shown_bytes, p.total_bytes),
                         (PREVIEW_LIMIT, PREVIEW_LIMIT + 1))


class TestExportPaging(WtTestCase):
    def _session(self, size, tag=None):
        store = store_with(root=self.tmp / f"store-{tag if tag is not None else size}")
        data = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
        priv = store.save_private(job_id="job-1", attempt_id="att-1",
                                  result_revision=1, section="content",
                                  relative_path="f.bin",
                                  media_type="application/octet-stream",
                                  data=data)
        sess = store.export_masked(private=priv, job_id="job-1",
                                   attempt_id="att-1", result_revision=1,
                                   relative_path="f.bin")
        return store, priv, sess, data

    def test_500k_exact_single_complete_page(self):
        store, priv, sess, data = self._session(EXPORT_CHUNK_LIMIT)
        out, pages = sess.drain()
        self.assertEqual(out, data)
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].is_last)
        self.assertEqual(pages[0].sha256, art.sha256_bytes(data))
        # full masked doc == source (no secrets) → export row complete
        row = store.sink.get(sess.record.artifact_id)
        self.assertEqual(row["complete"], 1)
        self.assertEqual(row["source_sha256"], priv.row["sha256"])

    def test_500k_plus_one_and_1mib_mixed_bounds(self):
        for size, npages, last_len in (
                (EXPORT_CHUNK_LIMIT + 1, 2, 1),
                (1_048_576, 3, 48_576)):
            store, priv, sess, data = self._session(size, tag=f"m{size}")
            out, pages = sess.drain()
            self.assertEqual(out, data, size)
            self.assertEqual(len(pages), npages, size)
            self.assertEqual(len(pages[-1].data), last_len, size)
            for i, pg in enumerate(pages):
                self.assertEqual(pg.sha256, art.sha256_bytes(
                    data[i * EXPORT_CHUNK_LIMIT:
                          (i + 1) * EXPORT_CHUNK_LIMIT]), size)
                self.assertFalse(pg.next_cursor is None and not pg.is_last)

    def test_truncated_page_can_never_be_complete(self):
        store = store_with(root=self.tmp / "store")
        data = b"A" * 100
        priv = store.save_private(job_id="job-1", attempt_id="att-1",
                                  result_revision=1, section="changes",
                                  relative_path="changes/p.patch",
                                  media_type="text/x-git-patch", data=data)
        with self.assertRaises(ArtifactError) as cm:
            store.save_export_page(private=priv, job_id="job-1",
                                   attempt_id="att-1", result_revision=1,
                                   relative_path="changes/p.page0",
                                   page_bytes=data[:10], complete=True)
        self.assertEqual(cm.exception.reason_code, REASON_TRUNCATED)
        rec = store.save_export_page(private=priv, job_id="job-1",
                                     attempt_id="att-1", result_revision=1,
                                     relative_path="changes/p.page0",
                                     page_bytes=data[:10], complete=False)
        self.assertEqual(rec.row["complete"], 0)
        self.assertEqual(rec.row["applyable"], 0)

    def test_one_mib_single_page_cap_forces_paging(self):
        store = store_with(root=self.tmp / "store")
        data = b"B" * (1_048_576 + 1)
        priv = store.save_private(job_id="job-1", attempt_id="att-1",
                                  result_revision=1, section="changes",
                                  relative_path="changes/big.patch",
                                  media_type="text/x-git-patch", data=data)
        with self.assertRaises(ArtifactError) as cm:
            store.save_export_page(private=priv, job_id="job-1",
                                   attempt_id="att-1", result_revision=1,
                                   relative_path="changes/big.page0",
                                   page_bytes=data, complete=False)
        self.assertEqual(cm.exception.reason_code, REASON_TOO_LARGE)
        # exact 1 MiB single page is allowed (bound inclusive)
        ok = store.save_private(job_id="job-1", attempt_id="att-1",
                                result_revision=1, section="changes",
                                relative_path="changes/exact.patch",
                                media_type="text/x-git-patch",
                                data=b"C" * 1_048_576)
        rec = store.save_export_page(private=ok, job_id="job-1",
                                     attempt_id="att-1", result_revision=1,
                                     relative_path="changes/exact.page0",
                                     page_bytes=b"C" * 1_048_576,
                                     complete=False)
        self.assertEqual(rec.row["byte_length"], 1_048_576)


class TestMaskingHashes(WtTestCase):
    def test_hash_recorded_after_masking(self):
        store = store_with(root=self.tmp / "store")
        raw = (b"token = \"ghp_" + b"S" * 36 + b"\"\n"
               + b"x" * 100 + b"\napi_key: " + b"K" * 20 + b"\n")
        priv = store.save_private(job_id="job-1", attempt_id="att-1",
                                  result_revision=1, section="content",
                                  relative_path="leaky.txt",
                                  media_type="text/plain", data=raw)
        sess = store.export_masked(private=priv, job_id="job-1",
                                   attempt_id="att-1", result_revision=1,
                                   relative_path="leaky.txt")
        row = store.sink.get(sess.record.artifact_id)
        masked = open(store._stored_path(row), "rb").read()
        self.assertEqual(sess.total_hits, 2)
        self.assertNotIn(b"ghp_", masked)
        self.assertNotIn(b"K" * 20, masked)
        self.assertIn(b"[REDACTED]", masked)
        # export sha is over MASKED bytes, source_sha pins the original
        self.assertEqual(row["sha256"], art.sha256_bytes(masked))
        self.assertEqual(row["source_sha256"], art.sha256_bytes(raw))
        self.assertNotEqual(row["sha256"], row["source_sha256"])
        self.assertEqual((row["redacted"], row["applyable"]), (1, 0))
        # chunk hash over the masked doc matches masked bytes
        out, pages = sess.drain()
        self.assertEqual(out, masked)
        self.assertEqual(art.sha256_bytes(out), row["sha256"])

    def test_redacted_never_applyable(self):
        sink = ListRegistrySink()
        with self.assertRaises(ArtifactError) as cm:
            sink._check({"section": "content", "visibility": "export",
                         "byte_length": 1, "sha256": "0" * 64,
                         "redacted": 1, "complete": 1, "applyable": 1,
                         "attempt_id": "a", "relative_path": "r"})
        self.assertEqual(cm.exception.reason_code, REASON_INVALID_ROW)


class TestCursorSafety(WtTestCase):
    def _sess(self):
        clock = Clock()
        store = store_with(clock=clock, root=self.tmp / "store")
        data = b"z" * (EXPORT_CHUNK_LIMIT + 10)
        priv = store.save_private(job_id="job-1", attempt_id="att-1",
                                  result_revision=1, section="content",
                                  relative_path="z.bin",
                                  media_type="application/octet-stream",
                                  data=data)
        sess = store.export_masked(private=priv, job_id="job-1",
                                   attempt_id="att-1", result_revision=1,
                                   relative_path="z.bin")
        return clock, sess

    def test_cursor_mutation_refused(self):
        clock, sess = self._sess()
        cur = sess.first_cursor()
        for tampered in (cur[:-1] + ("0" if cur[-1] != "0" else "1"),
                         cur.replace("|0|", "|1|"),   # page index swapped
                         "garbage"):
            with self.assertRaises(ArtifactError) as cm:
                sess.page(tampered)
            self.assertEqual(cm.exception.reason_code, REASON_CURSOR_MUTATED)

    def test_cursor_expiry(self):
        clock, sess = self._sess()
        cur = sess.first_cursor()
        clock.t += 901  # past TTL
        with self.assertRaises(ArtifactError) as cm:
            sess.page(cur)
        self.assertEqual(cm.exception.reason_code, REASON_CURSOR_EXPIRED)
        # fresh cursor after expiry works (not silently reused)
        p = sess.page(sess.first_cursor())
        self.assertEqual(p.index, 0)

    def test_cursor_stale_wrong_generation(self):
        clock, sess = self._sess()
        cur = sess.first_cursor()
        other = store_with(clock=clock, root=self.tmp / "store2")
        data = b"y" * 5
        priv2 = other.save_private(job_id="job-1", attempt_id="att-2",
                                   result_revision=1, section="content",
                                   relative_path="y.bin",
                                   media_type="text/plain", data=data)
        sess2 = other.export_masked(private=priv2, job_id="job-1",
                                    attempt_id="att-2", result_revision=1,
                                    relative_path="y.bin")
        # reusing session-1's cursor on session-2's artifact → STALE
        with self.assertRaises(ArtifactError) as cm:
            sess2.page(cur)
        self.assertEqual(cm.exception.reason_code, REASON_CURSOR_STALE)


class TestStoreDiscipline(WtTestCase):
    def test_order_private_before_export(self):
        store = store_with(root=self.tmp / "store")
        with self.assertRaises(ArtifactError) as cm:
            store.read_private("art-missing")
        self.assertEqual(cm.exception.reason_code, REASON_ORDER)

    def test_conflict_and_path_escape(self):
        store = store_with(root=self.tmp / "store")
        kw = dict(job_id="job-1", attempt_id="att-1", result_revision=1,
                  section="content", media_type="text/plain", data=b"x")
        store.save_private(relative_path="a.txt", **kw)
        with self.assertRaises(ArtifactError) as cm:
            store.save_private(relative_path="a.txt", **kw)
        self.assertEqual(cm.exception.reason_code, REASON_REGISTRY_CONFLICT)
        with self.assertRaises(ArtifactError) as cm:
            store.save_private(relative_path="../../../evil.txt", **kw)
        self.assertEqual(cm.exception.reason_code, REASON_INVALID_ROW)
        # file on disk is owner-only
        import stat as _stat
        import os as _os
        st = _os.stat(self.tmp / "store" / "private" / "att-1" / "a.txt")
        self.assertEqual(_stat.S_IMODE(st.st_mode), 0o600)

    def test_sqlite_sink_frozen_schema(self):
        conn = new_db()
        sink = SqliteRegistrySink(conn)
        store = store_with(sink=sink, root=self.tmp / "store")
        priv = store.save_private(job_id="job-1", attempt_id="att-1",
                                  result_revision=1, section="changes",
                                  relative_path="changes/p.patch",
                                  media_type="text/x-git-patch",
                                  data=b"diff --git\n", applyable=1)
        row = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?",
                           (priv.artifact_id,)).fetchone()
        self.assertEqual(row["complete"], 1)
        self.assertEqual(row["applyable"], 1)
        self.assertEqual(row["visibility"], "private")
        # schema CHECK: redacted+applyable impossible at DB level too
        with self.assertRaises(Exception):
            conn.execute(
                "INSERT INTO artifacts (artifact_id, job_id, attempt_id,"
                " result_revision, section, visibility, relative_path,"
                " media_type, byte_length, sha256, redacted, complete,"
                " applyable, created_at) VALUES ('x','job-1','att-1',1,"
                " 'content','export','r','text',1,?,1,1,1,'t')",
                ("0" * 64,))
