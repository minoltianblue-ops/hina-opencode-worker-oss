"""T12 — reviewer가 source snapshot/새 파일까지 정확히 재현, stale check
재사용 금지. (M1_WORK_PACKAGES.md T12; WORKSPACE.md ch4–ch5)

The reviewer receives ONLY: base_sha, the tracked patch artifact, the
changes manifest (with per-new-file artifact ids) and the source snapshot
hash. From a fresh worktree at base they must reproduce the writer's tree
byte-for-byte — new files included (ch5: new-file omission is BLOCKED).
"""
from __future__ import annotations

import json
import os

from hina_opencode_worker import artifacts as art
from hina_opencode_worker import workspace as ws
from hina_opencode_worker.artifacts import (
    ArtifactStore,
    REASON_SNAPSHOT_STALE,
    ArtifactError,
)
from hina_opencode_worker.workspace import DirtyInputPolicy

from tests._wt_support import WtTestCase, git, write

BIN = b"\x7fELF\x00\x01binary-ish\xff"


class TestReviewerExactRepro(WtTestCase):
    def setUp(self):
        super().setUp()
        # dirty original: staged + staged-new + unstaged + untracked(+Hangul)
        # + binary
        write(self.repo / "src" / "main.py", "print('staged edit')\n")
        git(self.repo, "add", "src/main.py")
        write(self.repo / "src" / "main.py", "print('worktree final')\n")
        write(self.repo / "src" / "newmod.py", "NEW_MODULE = True\n")
        git(self.repo, "add", "src/newmod.py")   # staged NEW (status A)
        write(self.repo / "README.md", "unstaged readme\n")
        write(self.repo / "docs" / "한글 스펙 v2.md", "스펙 내용\n")
        write(self.repo / "assets" / "app.bin", BIN)

    def _writer_submission(self):
        repo = ws.verify_repository(str(self.repo))
        self.store = ArtifactStore(self.tmp / "artifacts")
        snap = ws.prepare_inputs(
            repo, self.base_sha, DirtyInputPolicy(
                include_existing_changes=True,
                context_files=("src/main.py", "src/newmod.py", "README.md",
                               "docs/한글 스펙 v2.md", "assets/app.bin")))
        h = self.track_worktree(ws.create_worktree(
            repo, self.base_sha, pool_root=str(self.pool), job_id="job-1",
            attempt_id="att-1", executor_id="exec-1",
            sink=ws.ListLeaseSink()))
        ws.apply_inputs(h, snap)
        # --- writer does the job -----------------------------------------
        wt = h.canonical_path
        write(os.path.join(wt, "src", "main.py"),
              "print('worktree final + worker edit')\n")
        write(os.path.join(wt, "out", "report.txt"), "결과 리포트\n")
        write(os.path.join(wt, "out", "data.bin"), BIN)
        os.remove(os.path.join(wt, "notes", "README.md"))
        os.symlink("report.txt", os.path.join(wt, "out", "link.txt"))
        res = h.collect()
        # store private-first, then bind artifact ids into the manifest (ch4)
        for e in res["new_files"]:
            abs_p = os.path.join(wt, e["relative_path"])
            if e["file_type"] == "link":
                data = os.fsencode(os.readlink(abs_p))
            else:
                with open(abs_p, "rb") as f:
                    data = f.read()
            rec = self.store.save_private(
                job_id="job-1", attempt_id="att-1", result_revision=1,
                section="content", relative_path="new/" + e["relative_path"],
                media_type="application/octet-stream", data=data)
            e["artifact_id"] = rec.artifact_id
        patch_rec = self.store.save_private(
            job_id="job-1", attempt_id="att-1", result_revision=1,
            section="changes", relative_path="changes/tracked.patch",
            media_type="text/x-git-patch", data=res["tracked_patch"],
            applyable=1)
        doc_rec = self.store.save_private(
            job_id="job-1", attempt_id="att-1", result_revision=1,
            section="changes", relative_path="changes/manifest.json",
            media_type="application/json",
            data=art.canonical_json(res["manifest_doc"]))
        return h, res, {"patch_id": patch_rec.artifact_id,
                        "doc_id": doc_rec.artifact_id,
                        "source_snapshot_sha256":
                            res["source_snapshot_sha256"]}

    def _reviewer_repro(self, bundle, drop=None):
        review_root = self.tmp / "review-wt"
        git(self.repo, "worktree", "add", "--detach", str(review_root),
            self.base_sha)
        self.addCleanup(self._safe_rm_review, review_root)
        patch = self.store.read_private(bundle["patch_id"])
        git(review_root, "apply", "--binary", "--", input=patch)
        doc = self.store.read_private(bundle["doc_id"])
        entries = json.loads(doc)["entries"]
        for e in entries:
            if e["change_kind"] != "added" or not e.get("artifact_id"):
                continue
            if drop and e["relative_path"] == drop:
                continue  # simulate a dropped/missing new-file entry
            target = os.path.join(str(review_root), e["relative_path"])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            raw = self.store.read_private(e["artifact_id"])
            if os.path.lexists(target):
                # staged-new inputs are ALSO in the patch: the manifest may
                # carry them — reproduce means VERIFY, never overwrite.
                existing = (os.fsencode(os.readlink(target))
                            if os.path.islink(target) else
                            open(target, "rb").read())
                self.assertEqual(art.sha256_bytes(existing),
                                 e["after_sha256"], e["relative_path"])
                continue
            if e["file_type"] == "link":
                os.symlink(raw.decode("utf-8", "surrogateescape"), target)
            else:
                mode = 0o755 if e["mode"] == "100755" else 0o644
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             mode)
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
        return review_root

    def _safe_rm_review(self, root):
        if os.path.isdir(root):
            git(self.repo, "worktree", "remove", "--force", str(root),
                check=False)

    def test_reviewer_reproduces_writer_tree_exactly(self):
        h, res, bundle = self._writer_submission()
        review_root = self._reviewer_repro(bundle)
        rev_entries, rev_sha = art.tree_manifest(review_root)
        diff = art.snapshot_diff(bundle["source_snapshot_sha256"],
                                 res["tree_manifest"], rev_sha, rev_entries)
        self.assertTrue(diff["match"], diff)
        self.assertEqual(diff["missing"], [])
        self.assertEqual(diff["extra"], [])
        self.assertEqual(diff["changed"], [])
        # the reviewer even reproduces Hangul names, binary and link
        self.assertEqual(
            open(os.path.join(str(review_root), "docs",
                              "한글 스펙 v2.md"), encoding="utf-8").read(),
            "스펙 내용\n")
        with open(os.path.join(str(review_root), "out",
                               "data.bin"), "rb") as f:
            self.assertEqual(f.read(), BIN)
        self.assertEqual(os.readlink(os.path.join(str(review_root), "out",
                                                  "link.txt")), "report.txt")
        # patch hash and snapshot hash stay distinct concepts (ch4)
        self.assertNotEqual(res["manifest_doc"]["tracked_patch_sha256"],
                            bundle["source_snapshot_sha256"])

    def test_missing_new_file_is_blocked_not_passing(self):
        h, res, bundle = self._writer_submission()
        review_root = self._reviewer_repro(bundle, drop="out/data.bin")
        rev_entries, rev_sha = art.tree_manifest(review_root)
        diff = art.snapshot_diff(bundle["source_snapshot_sha256"],
                                 res["tree_manifest"], rev_sha, rev_entries)
        self.assertFalse(diff["match"])
        self.assertIn("out/data.bin", diff["missing"])  # BLOCKED, loud

    def test_stale_snapshot_check_cannot_pass_next_revision(self):
        h, res, bundle = self._writer_submission()
        rev1_entries, rev1_sha = dict(res["tree_manifest"]), \
            bundle["source_snapshot_sha256"]
        # writer produces revision 2 in the same worktree
        write(os.path.join(h.canonical_path, "out", "report.txt"),
              "결과 리포트 v2\n")
        rev2_entries, rev2_sha = art.tree_manifest(h.canonical_path)
        # reusing the revision-1 check against revision 2 must FAIL loudly,
        # never silently pass (ch5)
        diff = art.snapshot_diff(rev1_sha, rev1_entries, rev2_sha,
                                 rev2_entries)
        self.assertFalse(diff["match"])
        self.assertIn("out/report.txt", diff["changed"])

    def test_foreign_baseline_is_refused_as_stale_check(self):
        h = self.track_worktree(ws.create_worktree(
            ws.verify_repository(str(self.repo)), self.base_sha,
            pool_root=str(self.pool), job_id="job-1", attempt_id="att-1",
            executor_id="exec-1", sink=ws.ListLeaseSink()))
        ghost = {"from_other_revision.txt": {
            "path": "from_other_revision.txt", "tracked": True,
            "type": "file", "mode": "100644", "size": 1,
            "content_sha256": "0" * 64}}
        with self.assertRaises(ArtifactError) as cm:
            art.collect_changes(h.canonical_path, self.base_sha,
                                baseline=ghost, baseline_sha="f" * 64)
        self.assertEqual(cm.exception.reason_code, REASON_SNAPSHOT_STALE)
