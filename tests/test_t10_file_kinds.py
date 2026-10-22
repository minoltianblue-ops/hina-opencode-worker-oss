"""T10 — 신규/삭제/rename/binary/symlink/한글·공백·대소문자 충돌·gitlink.

(M1_WORK_PACKAGES.md 필수 자동화 테스트 T10; WORKSPACE.md ch3–ch4)
"""
from __future__ import annotations

import os

from hina_opencode_worker import artifacts as art
from hina_opencode_worker import workspace as ws
from hina_opencode_worker.workspace import (
    CAPABILITY_UNVERIFIED,
    DirtyInputPolicy,
    REASON_CASE_COLLISION,
    WorkspaceError,
)

from tests._wt_support import WtTestCase, git, write

BIN = b"\x00\x01\x02PNG-like bytes\xff\xfe\x00"


class WtFixture(WtTestCase):
    def setUp(self):
        super().setUp()
        self.repo_info = ws.verify_repository(str(self.repo))

    def make_wt(self):
        self.sink = ws.ListLeaseSink()
        h = ws.create_worktree(self.repo_info, self.base_sha,
                               pool_root=str(self.pool), job_id="job-1",
                               attempt_id="att-1", executor_id="exec-1",
                               sink=self.sink)
        return self.track_worktree(h)


class TestBinarySymlinkHangul(WtFixture):
    def _apply_kinds(self, h):
        write(self.repo / "assets" / "blob.bin", BIN)          # binary
        write(self.repo / "docs" / "한글 파일 이름.txt", "한글 내용\n")
        write(self.repo / "docs" / "두  공간  이름.txt", "spaces\n")
        (self.repo / "link-in").symlink_to("README.md")        # internal link
        (self.repo / "link-out").symlink_to("/etc/hosts")      # external link
        pol = DirtyInputPolicy(include_existing_changes=True,
                               context_files=("assets/blob.bin",
                                              "docs/한글 파일 이름.txt",
                                              "docs/두  공간  이름.txt",
                                              "link-in", "link-out"))
        snap = ws.prepare_inputs(self.repo_info, self.base_sha, pol)
        ws.apply_inputs(h, snap)
        return snap

    def test_binary_untracked_preserved_as_raw_bytes(self):
        h = self.make_wt()
        self._apply_kinds(h)
        got = open(os.path.join(h.canonical_path, "assets",
                                "blob.bin"), "rb").read()
        self.assertEqual(got, BIN)  # bytes archive path, not empty text

    def test_symlinks_recorded_as_links_not_dereferenced(self):
        h = self.make_wt()
        self._apply_kinds(h)
        wt = h.canonical_path
        for name, target in (("link-in", "README.md"),
                             ("link-out", "/etc/hosts")):
            p = os.path.join(wt, name)
            self.assertTrue(os.path.islink(p), name)
            self.assertEqual(os.readlink(p), target)
        # external target was NOT copied into the worktree
        self.assertFalse(os.path.exists(os.path.join(wt, "etc", "hosts")))

    def test_hangul_and_space_names_roundtrip(self):
        h = self.make_wt()
        self._apply_kinds(h)
        wt = h.canonical_path
        self.assertEqual(
            open(os.path.join(wt, "docs", "한글 파일 이름.txt"),
                 encoding="utf-8").read(), "한글 내용\n")
        self.assertEqual(
            open(os.path.join(wt, "docs", "두  공간  이름.txt"),
                 encoding="utf-8").read(), "spaces\n")

    def test_manifest_classifies_kinds(self):
        h = self.make_wt()
        self._apply_kinds(h)
        entries, sha = art.tree_manifest(h.canonical_path)
        self.assertEqual(entries["assets/blob.bin"]["type"], "file")
        self.assertEqual(entries["assets/blob.bin"]["content_sha256"],
                         art.sha256_bytes(BIN))
        self.assertEqual(entries["link-in"]["type"], "link")
        self.assertEqual(entries["link-in"]["mode"], "120000")
        self.assertEqual(entries["link-in"]["content_sha256"],
                         art.sha256_bytes(b"README.md"))  # link recorded
        self.assertEqual(entries["docs/한글 파일 이름.txt"]["type"], "file")
        # snapshot hash is stable & canonical
        e2, sha2 = art.tree_manifest(h.canonical_path)
        self.assertEqual(sha, sha2)


class TestCaseCollision(WtFixture):
    def test_input_collides_with_existing_tree_case_insensitive(self):
        # base has notes/README.md; a case-flavoured input would silently
        # overwrite it on case-insensitive filesystems → refuse (ch3)
        write(self.repo / "notes" / "readme.md", "sneaky different\n")
        pol = DirtyInputPolicy(include_existing_changes=True,
                               context_files=("notes/readme.md",))
        snap = ws.prepare_inputs(self.repo_info, self.base_sha, pol)
        h = self.make_wt()
        with self.assertRaises(WorkspaceError) as cm:
            ws.apply_inputs(h, snap)
        self.assertEqual(cm.exception.reason_code, REASON_CASE_COLLISION)

    def test_two_inputs_colliding_with_each_other(self):
        # on this case-insensitive FS we cannot have both files, but the
        # allowlist refuses before touching disk
        write(self.repo / "dup.txt", "one\n")
        pol = DirtyInputPolicy(include_existing_changes=True,
                               context_files=("dup.txt", "DUP.txt"))
        with self.assertRaises(WorkspaceError) as cm:
            ws.prepare_inputs(self.repo_info, self.base_sha, pol)
        self.assertEqual(cm.exception.reason_code, REASON_CASE_COLLISION)


class TestGitlink(WtFixture):
    def test_gitlink_reported_only_never_auto_initialized(self):
        h = self.make_wt()
        wt = h.canonical_path
        fake_oid = "a" * 40
        # simulate a submodule entry appearing during work (ch3: detect and
        # report only; first version must NOT init/modify submodules)
        git(wt, "update-index", "--add", "--cacheinfo",
            f"160000,{fake_oid},models/sub")
        res = h.collect()
        entry = [e for e in res["entries"]
                 if e["relative_path"] == "models/sub"]
        self.assertTrue(entry)
        self.assertEqual(entry[0]["file_type"], "gitlink")
        self.assertEqual(entry[0]["mode"], "160000")
        self.assertIn(CAPABILITY_UNVERIFIED, res["capability_flags"])
        # never auto-initialized: no checkout directory created
        self.assertFalse(os.path.exists(os.path.join(wt, "models", "sub")))


class TestDeleteRenameCollect(WtFixture):
    def test_deleted_and_renamed_entries_keep_both_paths_and_hashes(self):
        h = self.make_wt()
        ws.apply_inputs(h, ws.prepare_inputs(
            self.repo_info, self.base_sha, DirtyInputPolicy(
                use_committed_only=True)))
        wt = h.canonical_path
        os.remove(os.path.join(wt, "notes", "README.md"))     # deletion
        git(wt, "mv", "src/main.py", "src/app.py")             # rename
        write(os.path.join(wt, "out.txt"), "new file\n")       # new file
        res = h.collect()
        by_path = {e["relative_path"]: e for e in res["entries"]}

        dele = by_path["notes/README.md"]
        self.assertEqual(dele["change_kind"], "deleted")
        self.assertIsNotNone(dele["before_sha256"])
        self.assertIsNone(dele["after_sha256"])   # never replaced by ""
        self.assertNotEqual(dele["file_type"], "text-empty")

        ren = by_path["src/app.py"]
        self.assertEqual(ren["change_kind"], "renamed")
        self.assertEqual(ren["old_path"], "src/main.py")      # both paths
        self.assertIsNotNone(ren["before_sha256"])
        self.assertIsNotNone(ren["after_sha256"])

        newf = by_path["out.txt"]
        self.assertEqual(newf["origin"], "writer")
        self.assertEqual(newf["change_kind"], "added")
        self.assertIsNone(newf["before_sha256"])

        # patch hash is DISTINCT from the source snapshot hash (ch4)
        self.assertNotEqual(res["manifest_doc"]["tracked_patch_sha256"],
                            res["source_snapshot_sha256"])
        self.assertTrue(res["manifest_doc"]["tracked_patch_sha256"])

    def test_writer_new_untracked_binary_not_empty(self):
        h = self.make_wt()
        ws.apply_inputs(h, ws.prepare_inputs(
            self.repo_info, self.base_sha, DirtyInputPolicy(
                use_committed_only=True)))
        write(os.path.join(h.canonical_path, "dist", "data.bin"), BIN)
        res = h.collect()
        e = {x["relative_path"]: x for x in res["entries"]}["dist/data.bin"]
        self.assertEqual(e["origin"], "writer")
        self.assertEqual(e["after_sha256"], art.sha256_bytes(BIN))
        self.assertEqual(e["size"], len(BIN))   # binary kept as bytes+hash


class TestExclusionsAndIgnored(WtFixture):
    def test_ignored_requested_collected_others_reported_not_silent(self):
        h = self.make_wt()
        ws.apply_inputs(h, ws.prepare_inputs(
            self.repo_info, self.base_sha, DirtyInputPolicy(
                use_committed_only=True)))
        wt = h.canonical_path
        write(os.path.join(wt, "build", "cache.o"), "junk\n")
        write(os.path.join(wt, "build", "skip.log"), "more junk\n")
        write(os.path.join(wt, "build", "deliverable.txt"), "output\n")
        write(os.path.join(wt, ".gitignore"),
              "build/*\n!build/deliverable.txt\n")
        # deliverable un-ignored via negation; cache/skip stay ignored
        res = h.collect(expected_ignored=("build/cache.o",))
        by_path = {e["relative_path"]: e for e in res["entries"]}
        self.assertIn("build/deliverable.txt", by_path)   # un-ignored, normal
        self.assertEqual(by_path["build/deliverable.txt"]["origin"],
                         "writer")
        ign = by_path["build/cache.o"]
        self.assertEqual(ign["origin"], "ignored_requested")  # explicit
        # other ignored files are EXPLICIT in the exclusion policy (ch3),
        # never silently dropped
        self.assertEqual(
            res["manifest_doc"]["excluded_policy"]["ignored_not_requested"],
            ["build/skip.log"])
        self.assertIn(".gitignore", by_path)
