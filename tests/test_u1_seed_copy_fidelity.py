"""U1 — 비Git 선택자료 seed 복사 충실도 + 원본 미변경 증명 + 선언된 제외
(WORKSPACE.md ch7; M2_WORK_PACKAGES.md Q1).

copy fidelity: seed bytes/modes/links == original bytes, re-hashed after
write; exclusions DECLARED (cache dir + secret file) and never copied;
anything neither selected nor declared aborts (SEED_UNDECLARED_INPUT —
never silently skipped). Original untouched is proven by hash manifest
compare across the FULL flow (seed → worker WT → collect → apply-back).
Non-git capability defaults to false; only an explicit per-task profile
turns it on.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from hina_opencode_worker import seedrun as sr
from hina_opencode_worker.workspace import (
    CAPABILITY_UNVERIFIED, REASON_EXISTS, REASON_INVALID_POLICY,
    REASON_UNSAFE_PATH, SEED_UNDECLARED_INPUT, SeedCapability,
    WorkspaceError,
)
from tests._seed_support import (
    CACHE, DEFAULT_FILES, SECRET, SeedTestCase, default_policy,
    enabled, write,
)


class TestSeedCapabilityGate(SeedTestCase):
    def test_default_false_and_per_task_profile_only(self):
        with self.assertRaises(WorkspaceError) as cm:
            sr.create_seed_repo(self.original, default_policy(),
                                seed_path=self.tmp / "seed",
                                capability=None)
        self.assertEqual(cm.exception.reason_code, CAPABILITY_UNVERIFIED)
        with self.assertRaises(WorkspaceError) as cm:
            sr.create_seed_repo(self.original, default_policy(),
                                seed_path=self.tmp / "seed",
                                capability=SeedCapability())
        self.assertEqual(cm.exception.reason_code, CAPABILITY_UNVERIFIED)

        # enabled ONLY by explicit boolean true on the task profile
        self.assertFalse(
            SeedCapability.from_task_profile(None).non_git)
        self.assertFalse(
            SeedCapability.from_task_profile({}).non_git)
        self.assertFalse(
            SeedCapability.from_task_profile({"non_git": "yes"}).non_git)
        self.assertFalse(
            SeedCapability.from_task_profile({"non_git": 1}).non_git)
        self.assertTrue(
            SeedCapability.from_task_profile({"non_git": True}).non_git)

        # the profile-derived capability is what unlocks the flow
        cap = SeedCapability.from_task_profile({"non_git": True})
        seed = sr.create_seed_repo(self.original, default_policy(),
                                   seed_path=self.tmp / "seed",
                                   capability=cap)
        self.assertEqual(len(seed.copied), len(DEFAULT_FILES))


class TestSeedCopyFidelity(SeedTestCase):
    def _seed(self):
        return sr.create_seed_repo(self.original, default_policy(),
                                   seed_path=self.tmp / "seed",
                                   capability=enabled())

    def test_bytes_modes_and_links_match_original(self):
        seed = self._seed()
        by_path = {c["path"]: c for c in seed.copied}
        self.assertEqual(set(by_path), set(DEFAULT_FILES))
        # regular file content + exec bit
        self.assertEqual(Path(seed.seed_path, "notes", "spec.md").read_bytes(),
                         b"spec v1\n")
        st = os.lstat(Path(seed.seed_path, "scripts", "run.sh"))
        self.assertTrue(st.st_mode & 0o111, "exec bit must survive")
        st2 = os.lstat(Path(seed.seed_path, "notes", "spec.md"))
        self.assertFalse(st2.st_mode & 0o111)
        # symlink copied as a link, target recorded not dereferenced
        link = Path(seed.seed_path, "latest")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), os.path.join("data", "input.csv"))
        # every copied entry re-hashed to the original sha at copy time
        for rel, entry in seed.original_manifest.items():
            if rel in by_path:
                self.assertEqual(by_path[rel]["content_sha256"],
                                 entry["content_sha256"])

    def test_exclusions_declared_and_never_copied(self):
        seed = self._seed()
        git_ls = sr._seed_git(seed.seed_path, "ls-files", "-z").stdout.decode()
        self.assertIn("notes/spec.md", git_ls)
        self.assertNotIn("cache/blob.bin", git_ls)
        self.assertNotIn("secret.env", git_ls)
        self.assertFalse(Path(seed.seed_path, "cache").exists())
        self.assertFalse(Path(seed.seed_path, "secret.env").exists())
        report = {r["declaration"]: r for r in seed.exclusion_report}
        self.assertEqual(report["cache"]["kind"], "dir")
        self.assertEqual(report["secret.env"]["kind"], "file")
        # declared exclusions are recorded, and the bytes are untouched too
        self.assertEqual((self.original / "cache" / "blob.bin").read_bytes(),
                         CACHE)
        self.assertEqual((self.original / "secret.env").read_bytes(), SECRET)

    def test_undecided_original_file_aborts_never_skipped(self):
        write(self.original / "stray.txt", b"forgot me\n")
        with self.assertRaises(WorkspaceError) as cm:
            self._seed()
        self.assertEqual(cm.exception.reason_code, SEED_UNDECLARED_INPUT)
        self.assertIn("stray.txt", str(cm.exception))
        # declaring it makes the flow proceed and the report says so
        policy = replace(default_policy(),
                         exclusions=("cache", "secret.env", "stray.txt"))
        seed = sr.create_seed_repo(self.original, policy,
                                   seed_path=self.tmp / "seed",
                                   capability=enabled())
        report = {r["declaration"]: r for r in seed.exclusion_report}
        self.assertEqual(report["stray.txt"]["matched"], 1)

    def test_original_untouched_hash_compare_across_full_flow(self):
        from hina_opencode_worker.workspace import ListLeaseSink
        before_sha = sr.original_manifest_sha256(
            sr.original_hash_manifest(self.original))
        seed = self._seed()
        handle = self.track_seed_wt(sr.seed_workspace(
            seed, pool_root=str(self.pool), job_id="u1-flow", sink=ListLeaseSink()))
        wt = Path(handle.canonical_path)
        write(wt / "notes" / "spec.md", b"spec v2 edited\n")
        write(wt / "output" / "result.txt", b"result 42\n")
        work = sr.collect_worker_result(handle)
        sr.apply_back_to_seed(seed, work)
        # proof: the original hash manifest is identical after the whole flow
        self.assertEqual(
            sr.original_manifest_sha256(
                sr.original_hash_manifest(self.original)),
            before_sha)
        # and worker edits never leaked into the original
        self.assertEqual((self.original / "notes" / "spec.md").read_bytes(),
                         b"spec v1\n")
        self.assertFalse((self.original / "output").exists())

    def test_seed_base_sha_deterministic(self):
        a = self._seed()
        b = sr.create_seed_repo(self.original, default_policy(),
                                seed_path=self.tmp / "seed2",
                                capability=enabled())
        self.assertEqual(a.seed_base_sha, b.seed_base_sha)
        self.assertEqual(a.seed_tree_sha256, b.seed_tree_sha256)

    def test_seed_path_guard_and_no_overwrite(self):
        with self.assertRaises(WorkspaceError) as cm:
            sr.create_seed_repo(self.original, default_policy(),
                                seed_path=self.original / "inner-seed",
                                capability=enabled())
        self.assertEqual(cm.exception.reason_code, REASON_UNSAFE_PATH)
        pre = self.tmp / "existing"
        pre.mkdir()
        with self.assertRaises(WorkspaceError) as cm:
            sr.create_seed_repo(self.original, default_policy(),
                                seed_path=pre, capability=enabled())
        self.assertEqual(cm.exception.reason_code, REASON_EXISTS)

    def test_stale_or_contradictory_exclusion_refused(self):
        with self.assertRaises(WorkspaceError) as cm:
            sr.create_seed_repo(
                self.original,
                replace(default_policy(),
                        exclusions=("cache", "secret.env", "nothere")),
                seed_path=self.tmp / "seed", capability=enabled())
        self.assertEqual(cm.exception.reason_code, REASON_INVALID_POLICY)
        with self.assertRaises(WorkspaceError) as cm:
            sr.create_seed_repo(
                self.original,
                replace(default_policy(),
                        files=DEFAULT_FILES + ("secret.env",)),
                seed_path=self.tmp / "seed", capability=enabled())
        self.assertEqual(cm.exception.reason_code, REASON_INVALID_POLICY)
