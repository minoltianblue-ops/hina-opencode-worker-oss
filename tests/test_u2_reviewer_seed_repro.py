"""U2 — seed 위 worker/reviewer Worktree 정확 재현 (WORKSPACE.md ch5+ch7).

worker WT와 reviewer WT가 SEED 위에서创建된다: worker는 seed WT에서
patch + 새 파일을 만들고, seed 자체로 apply-back은 `git apply --check`
게이트를 통과해야만 반영된다. reviewer는 같은 original+policy에서 seed를
다시 복사(base SHA 일치 확인)하고 raw patch + 모든 새 파일을 재현해서
writer 트리 스냅샷과 정확히 일치해야 PASS — 새 파일 누락이나 컨텍스트
불일치 패치는 SEED_REPRO_MISMATCH / REASON_APPLY로 막힌다(ch5 BLOCKED).
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from hina_opencode_worker import artifacts as art
from hina_opencode_worker import seedrun as sr
from hina_opencode_worker.workspace import (
    ListLeaseSink, REASON_APPLY, REASON_REF_INVALID, SEED_REPRO_MISMATCH,
    WorkspaceError,
)
from tests._seed_support import SeedTestCase, default_policy, enabled, write


def _worker_edits(wt: Path):
    write(wt / "notes" / "spec.md", b"spec v2 edited\n")
    write(wt / "output" / "result.txt", b"result 42\n")
    write(wt / "output" / "tool.sh", b"#!/bin/sh\necho out\n")
    os.chmod(wt / "output" / "tool.sh", 0o755)
    (wt / "output" / "latest").symlink_to("result.txt")


class TestSeedWorkerReviewerRepro(SeedTestCase):
    def setUp(self):
        super().setUp()
        self.sink = ListLeaseSink()
        self.seed = sr.create_seed_repo(self.original, default_policy(),
                                        seed_path=self.tmp / "seed",
                                        capability=enabled(),
                                        sink=self.sink)
        self.handle = self.track_seed_wt(sr.seed_workspace(
            self.seed, pool_root=str(self.pool), job_id="u2",
            sink=self.sink))
        _worker_edits(Path(self.handle.canonical_path))
        self.work = sr.collect_worker_result(self.handle)

    def test_collect_carries_patch_and_all_new_files(self):
        self.assertTrue(self.work.patch)
        new_paths = sorted(c.path for c in self.work.new_files)
        self.assertEqual(new_paths,
                         ["output/latest", "output/result.txt",
                          "output/tool.sh"])
        kinds = {c.path: c.kind for c in self.work.new_files}
        self.assertEqual(kinds["output/latest"], "symlink")
        self.assertEqual(kinds["output/tool.sh"], "untracked")
        manifest_new = sorted(e["relative_path"]
                              for e in self.work.changes["new_files"])
        self.assertEqual(manifest_new, new_paths)

    def test_apply_back_check_gate_reproduces_worker_tree(self):
        entries, sha = sr.apply_back_to_seed(self.seed, self.work)
        self.assertEqual(sha, self.work.tree_sha256)
        # applying the same result again is refused by --check, not repeated
        with self.assertRaises(WorkspaceError) as cm:
            sr.apply_back_to_seed(self.seed, self.work)
        self.assertEqual(cm.exception.reason_code, REASON_APPLY)

    def test_reviewer_reproduces_seed_result_exactly(self):
        res = sr.reviewer_seed_repro(
            self.original, default_policy(), self.work,
            expected=self.seed, seed_path=self.tmp / "seed-r",
            pool_root=str(self.pool), job_id="u2",
            sink=self.sink, capability=enabled())
        self.track_seed_wt(res["handle"])
        self.assertTrue(res["match"])
        self.assertEqual(res["seed_base_sha"], self.seed.seed_base_sha)
        self.assertEqual(res["snapshot_sha256"], self.work.tree_sha256)
        # reviewer WT is a detached worktree ON the reviewer's seed
        self.assertEqual(str(Path(res["reviewer_worktree"]).parent.parent),
                         str(self.pool.resolve()))

    def test_reviewer_blocked_when_new_file_missing(self):
        stripped = replace(self.work, new_files=[])
        with self.assertRaises(WorkspaceError) as cm:
            sr.reviewer_seed_repro(
                self.original, default_policy(), stripped,
                expected=self.seed, seed_path=self.tmp / "seed-r",
                pool_root=str(self.pool), job_id="u2",
                sink=self.sink, capability=enabled())
        self.assertEqual(cm.exception.reason_code, SEED_REPRO_MISMATCH)
        self.assertIn("missing", str(cm.exception))

    def test_tampered_patch_refused_by_check_seed_untouched(self):
        _entries, before_sha = art.tree_manifest(self.seed.seed_path)
        bad = replace(
            self.work, patch=(
                b"diff --git a/notes/spec.md b/notes/spec.md\n"
                b"index 8ba3d10..1111111 100644\n"
                b"--- a/notes/spec.md\n"
                b"+++ b/notes/spec.md\n"
                b"@@ -1,1 +1,1 @@\n"
                b"-spec v9\n"
                b"+spec tampered\n"))
        with self.assertRaises(WorkspaceError) as cm:
            sr.apply_back_to_seed(self.seed, bad)
        self.assertEqual(cm.exception.reason_code, REASON_APPLY)
        _entries, after_sha = art.tree_manifest(self.seed.seed_path)
        self.assertEqual(after_sha, before_sha,
                         "rejected patch must leave the seed byte-identical")

    def test_apply_back_onto_moved_seed_head_refused(self):
        sr._seed_git(self.seed.seed_path, "commit", "--allow-empty", "-q",
                     "-m", "seed drifted off base")
        with self.assertRaises(WorkspaceError) as cm:
            sr.apply_back_to_seed(self.seed, self.work)
        self.assertEqual(cm.exception.reason_code, REASON_REF_INVALID)
