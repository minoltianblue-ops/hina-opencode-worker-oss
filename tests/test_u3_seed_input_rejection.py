"""U3 — unsafe path / case-collision / 비Git 전제 위반 거절
(WORKSPACE.md ch3+ch7; validate_relpath 시드 흐름 확장).

allowlist 입력은 ch3 규칙 그대로: `..`/absolute/drive/backslash/빈 segment/
`.git`/glob 거절, case-insensitive 충돌 거절. seed 적용 직전(new file)에도
동일 검사를 재적용(depth defense). git 저장은 seed 저장소일 때만 가능하고
git 저장소 original은 거부(SEED_ORIGINAL_IS_GIT), special file은 복사 불가라
제외 선언을 요구한다.
"""
from __future__ import annotations

import os

from hina_opencode_worker import seedrun as sr
from hina_opencode_worker.artifacts import sha256_bytes
from hina_opencode_worker.seedrun import SeedWorkResult
from hina_opencode_worker.workspace import (
    CapturedInput, REASON_CASE_COLLISION, REASON_REFUSED,
    REASON_UNSAFE_PATH, SEED_ORIGINAL_IS_GIT, SEED_UNDECLARED_INPUT,
    WorkspaceError,
)
from tests._seed_support import SeedTestCase, default_policy, enabled, write

UNSAFE = ["../escape", "notes/../../escape", "/abs/file", "back\\slash",
          "a/./b", "a//b", ".git/config", "notes/.git/hooks", "glob*.md",
          "", "drive:C\\x"]


class TestSeedUnsafeInput(SeedTestCase):
    def _attempt(self, files=None, exclusions=None, seed="seed"):
        from dataclasses import replace as _replace
        policy = default_policy()
        if files is not None:
            policy = _replace(policy, files=tuple(files))
        if exclusions is not None:
            policy = _replace(policy, exclusions=tuple(exclusions))
        return sr.create_seed_repo(self.original, policy,
                                   seed_path=self.tmp / seed,
                                   capability=enabled())

    def test_unsafe_allowlist_entries_refused(self):
        for bad in UNSAFE:
            with self.subTest(bad=bad):
                with self.assertRaises(WorkspaceError) as cm:
                    self._attempt(files=tuple(
                        sorted(set(default_policy().files) | {bad})))
                self.assertEqual(cm.exception.reason_code,
                                 REASON_UNSAFE_PATH)

    def test_case_collision_refused(self):
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt(files=tuple(default_policy().files)
                          + ("docs/readme.md",))
        self.assertEqual(cm.exception.reason_code, REASON_CASE_COLLISION)
        # file vs declared exclusion, same collision rule
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt(files=("notes/README.md",),
                          exclusions=("NOTES/readme.md",))
        self.assertEqual(cm.exception.reason_code, REASON_CASE_COLLISION)

    def test_missing_selected_material_refused(self):
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt(files=("nope/missing.md", "data/input.csv"),
                          exclusions=("cache", "secret.env",
                                       "notes/spec.md", "scripts/run.sh",
                                       "docs/Readme.md", "latest"))
        self.assertEqual(cm.exception.reason_code, REASON_REFUSED)

    def test_git_original_refused_for_seed_flow(self):
        (self.original / ".git").mkdir()
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt()
        self.assertEqual(cm.exception.reason_code, SEED_ORIGINAL_IS_GIT)

    def test_special_file_must_be_declared_exclusion(self):
        os.mkfifo(self.original / "pipe.fifo")
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt()
        self.assertEqual(cm.exception.reason_code, SEED_UNDECLARED_INPUT)
        seed = self._attempt(exclusions=("cache", "secret.env", "pipe.fifo"))
        # declared: excluded from copy; original untouched
        ls = sr._seed_git(seed.seed_path, "ls-files", "-z").stdout.decode()
        self.assertNotIn("pipe.fifo", ls)

    def test_globish_or_dotgit_names_stay_declarable(self):
        # exclusion declarations are literal prefixes, so EVERY original
        # entry remains declarable — no entry can force a silent skip or a
        # dead end: undeclared first, then exact-literal exclusion works
        write(self.original / "notes" / "[draft]*.md", b"weird name\n")
        (self.original / "vendor" / ".git").mkdir(parents=True)
        write(self.original / "vendor" / ".git" / "HEAD",
              b"ref: refs/heads/x\n")
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt()
        self.assertEqual(cm.exception.reason_code, SEED_UNDECLARED_INPUT)
        seed = self._attempt(exclusions=("cache", "secret.env",
                                         "notes/[draft]*.md",
                                         "vendor/.git"))
        ls = sr._seed_git(seed.seed_path, "ls-files", "-z").stdout.decode()
        self.assertNotIn("vendor/.git", ls)
        self.assertNotIn("[draft]", ls)
        # but such a name is still NOT selectable (ch8: no glob selection)
        with self.assertRaises(WorkspaceError) as cm:
            self._attempt(files=tuple(default_policy().files)
                          + ("notes/[draft]*.md",))
        self.assertEqual(cm.exception.reason_code, REASON_UNSAFE_PATH)

    def test_new_file_unsafe_path_refused_at_apply_back(self):
        seed = self._attempt()
        evil = CapturedInput("../evil", "untracked", b"x", None, "100644",
                             1, sha256_bytes(b"x"))
        work = SeedWorkResult(seed_base_sha=seed.seed_base_sha, patch=b"",
                              new_files=[evil], changes={},
                              tree_sha256="", tree_entries={})
        with self.assertRaises(WorkspaceError) as cm:
            sr.apply_back_to_seed(seed, work)
        self.assertEqual(cm.exception.reason_code, REASON_UNSAFE_PATH)
        self.assertFalse((self.tmp / "evil").exists())

    def test_new_file_case_collision_with_seed_refused(self):
        seed = self._attempt()
        evil = CapturedInput("docs/readme.md", "untracked", b"lower", None,
                             "100644", 5, sha256_bytes(b"lower"))
        work = SeedWorkResult(seed_base_sha=seed.seed_base_sha, patch=b"",
                              new_files=[evil], changes={},
                              tree_sha256="", tree_entries={})
        with self.assertRaises(WorkspaceError) as cm:
            sr.apply_back_to_seed(seed, work)
        self.assertEqual(cm.exception.reason_code, REASON_CASE_COLLISION)
        # the real seed entry keeps its exact case and content
        with open(os.path.join(seed.seed_path, "docs", "Readme.md"),
                  "rb") as f:
            self.assertEqual(f.read(), b"top readme\n")
