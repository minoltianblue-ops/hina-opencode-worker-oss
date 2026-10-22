"""T09 — dirty 원본·staged/unstaged/untracked 보존, 원본 index 불변.

(M1_WORK_PACKAGES.md 필수 자동화 테스트 T09; WORKSPACE.md ch1–ch2)
"""
from __future__ import annotations

import os

from hina_opencode_worker import workspace as ws
from hina_opencode_worker.workspace import (
    CONTRACT_MISMATCH,
    DirtyInputPolicy,
    REASON_INVALID_POLICY,
    REASON_REF_INVALID,
    REASON_REFUSED,
    REASON_UNSAFE_PATH,
    WORKSPACE_CHANGED,
    WORKSPACE_DIRTY,
    WorkspaceError,
)

from tests._wt_support import WtTestCase, git, write


def dirty_repo(t):
    """staged change + staged→further-unstaged change + untracked file."""
    write(t.repo / "README.md", "unstaged edit\n")          # unstaged
    write(t.repo / "src" / "main.py", "print('staged')\n")
    git(t.repo, "add", "src/main.py")                        # staged
    write(t.repo / "src" / "main.py", "print('final work tree state')\n")
    write(t.repo / "src" / "util.py", "HELP = True\n")       # untracked
    return t.repo


class TestDirtyPolicy(WtTestCase):
    def _repo(self):
        return ws.verify_repository(str(self.repo))

    def test_dirty_without_decision_is_refused(self):
        dirty_repo(self)
        repo = self._repo()
        with self.assertRaises(WorkspaceError) as cm:
            ws.prepare_inputs(repo, self.base_sha, DirtyInputPolicy())
        self.assertEqual(cm.exception.reason_code, WORKSPACE_DIRTY)

    def test_verify_repository_requires_exact_root(self):
        repo = self._repo()
        self.assertEqual(repo.toplevel, repo.path)
        self.assertTrue(os.path.realpath(repo.common_dir))
        # subdir is refused — no prefix guessing (ch1)
        with self.assertRaises(WorkspaceError) as cm:
            ws.verify_repository(str(self.repo / "src"))
        self.assertEqual(cm.exception.reason_code, REASON_REFUSED)

    def test_resolve_ref_pins_commit_and_refuses_option_injection(self):
        repo = self._repo()
        self.assertEqual(ws.resolve_ref(repo, "main"), self.base_sha)
        self.assertEqual(ws.resolve_ref(repo, "main^{commit}"), self.base_sha)
        for evil in ("--help", "--output=/tmp/pwn", "-D", "  ", "",
                     "no-such-ref-xyz"):
            with self.assertRaises(WorkspaceError) as cm:
                ws.resolve_ref(repo, evil)
            self.assertEqual(cm.exception.reason_code, REASON_REF_INVALID)
        # probe: current git supports it; fallback validation still holds
        self.assertIsInstance(ws.end_of_options_supported(repo.path), bool)

    def test_policy_conflicts(self):
        repo = self._repo()
        cases = [
            DirtyInputPolicy(include_existing_changes=True,
                             use_committed_only=True),
            DirtyInputPolicy(include_existing_changes=True,
                             context_files=()),
            DirtyInputPolicy(use_committed_only=True,
                             context_files=("README.md",)),
        ]
        for pol in cases:
            with self.assertRaises(WorkspaceError) as cm:
                ws.prepare_inputs(repo, self.base_sha, pol)
            self.assertEqual(cm.exception.reason_code, REASON_INVALID_POLICY)

    def test_context_path_validation(self):
        dirty_repo(self)
        repo = self._repo()
        bad = [
            ("../outside.txt", REASON_UNSAFE_PATH),
            ("/etc/passwd", REASON_UNSAFE_PATH),
            ("src/../../x.txt", REASON_UNSAFE_PATH),
            (".git/config", REASON_UNSAFE_PATH),
            ("src\\evil.txt", REASON_UNSAFE_PATH),
            ("src/*", REASON_UNSAFE_PATH),           # glob unsupported (ch8)
            ("src", REASON_REFUSED),                 # directory unsupported
            ("missing.txt", REASON_REFUSED),
        ]
        for rel, code in bad:
            pol = DirtyInputPolicy(include_existing_changes=True,
                                   context_files=(rel,))
            with self.assertRaises(WorkspaceError) as cm:
                ws.prepare_inputs(repo, self.base_sha, pol)
            self.assertEqual(cm.exception.reason_code, code, rel)

    def test_include_existing_preserves_all_three_kinds(self):
        dirty_repo(self)
        write(self.repo / "src" / "exec.sh", "#!/bin/sh\necho hi\n")
        os.chmod(self.repo / "src" / "exec.sh", 0o755)
        repo = self._repo()
        pol = DirtyInputPolicy(
            include_existing_changes=True,
            context_files=("README.md", "src/main.py", "src/util.py",
                           "src/exec.sh"))
        snap = ws.prepare_inputs(repo, self.base_sha, pol)
        self.assertTrue(snap.is_dirty)
        self.assertTrue(snap.patch)  # tracked selections → patch

        h = ws.create_worktree(repo, self.base_sha, pool_root=str(self.pool),
                               job_id="job-1", attempt_id="att-1",
                               executor_id="exec-1", sink=ws.ListLeaseSink())
        try:
            ws.apply_inputs(h, snap)
            wt = h.canonical_path
            # staged+unstaged combined = final working state (ch3)
            self.assertEqual(
                open(os.path.join(wt, "src", "main.py")).read(),
                "print('final work tree state')\n")
            self.assertEqual(open(os.path.join(wt, "README.md")).read(),
                             "unstaged edit\n")
            self.assertEqual(open(os.path.join(wt, "src", "util.py")).read(),
                             "HELP = True\n")
            self.assertTrue(os.access(os.path.join(wt, "src", "exec.sh"),
                                      os.X_OK))
            # unselected dirty file would have to be refused upstream —
            # here nothing else was dirty; worktree starts at base otherwise
            self.assertEqual(
                open(os.path.join(wt, "notes", "README.md")).read(),
                "notes base\n")
            lease = h.lease_row
            self.assertEqual(lease["lease_state"], "held")
            self.assertEqual(lease["fencing_token"], 1)
            self.assertEqual(lease["owner_pid"], os.getpid())
            self.assertEqual(lease["git_common_dir"], repo.common_dir)
            self.assertEqual(lease["base_revision"], self.base_sha)
            self.assertTrue(h.sink.leases)
            kinds = {"attempt_intent", "control_lock"}
            self.assertTrue(kinds <= {k for k, _ in h.sink.notes})
        finally:
            git(self.repo, "worktree", "remove", "--force", h.canonical_path)
            h.release_lease()

    def test_original_index_and_status_untouched(self):
        dirty_repo(self)
        idx_before = self.index_bytes()
        status_before = self.status_bytes()
        stash_before = git(self.repo, "stash", "list").stdout
        head_before = git(self.repo, "rev-parse", "HEAD").stdout

        repo = self._repo()
        pol = DirtyInputPolicy(include_existing_changes=True,
                               context_files=("src/main.py", "src/util.py"))
        snap = ws.prepare_inputs(repo, self.base_sha, pol)
        h = ws.create_worktree(repo, self.base_sha, pool_root=str(self.pool),
                               job_id="job-1", attempt_id="att-1",
                               executor_id="exec-1", sink=ws.ListLeaseSink())
        try:
            ws.apply_inputs(h, snap)
        finally:
            git(self.repo, "worktree", "remove", "--force", h.canonical_path)
            h.release_lease()

        # original index byte-identical, status identical, no stash/commit
        self.assertEqual(self.index_bytes(), idx_before)
        self.assertEqual(self.status_bytes(), status_before)
        self.assertEqual(git(self.repo, "stash", "list").stdout,
                         stash_before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout,
                         head_before)

    def test_use_committed_only_skips_dirty_changes(self):
        dirty_repo(self)
        repo = self._repo()
        snap = ws.prepare_inputs(repo, self.base_sha,
                                 DirtyInputPolicy(use_committed_only=True))
        h = ws.create_worktree(repo, self.base_sha, pool_root=str(self.pool),
                               job_id="job-1", attempt_id="att-1",
                               executor_id="exec-1", sink=ws.ListLeaseSink())
        try:
            ws.apply_inputs(h, snap)
            self.assertEqual(
                open(os.path.join(h.canonical_path, "src",
                                  "main.py")).read(),
                "print('base')\n")   # committed state only
            self.assertFalse(os.path.exists(
                os.path.join(h.canonical_path, "src", "util.py")))
        finally:
            git(self.repo, "worktree", "remove", "--force", h.canonical_path)
            h.release_lease()

    def test_concurrent_original_change_is_workspaces_changed(self):
        dirty_repo(self)
        repo = self._repo()
        pol = DirtyInputPolicy(include_existing_changes=True,
                               context_files=("src/main.py",))
        snap = ws.prepare_inputs(repo, self.base_sha, pol)
        write(self.repo / "sneaky-new.txt", "changed after capture\n")
        h = ws.create_worktree(repo, self.base_sha, pool_root=str(self.pool),
                               job_id="job-1", attempt_id="att-1",
                               executor_id="exec-1", sink=ws.ListLeaseSink())
        try:
            with self.assertRaises(WorkspaceError) as cm:
                ws.apply_inputs(h, snap)
            self.assertEqual(cm.exception.reason_code, WORKSPACE_CHANGED)
        finally:
            git(self.repo, "worktree", "remove", "--force", h.canonical_path)
            h.release_lease()

    def test_lease_conflict_and_fencing_increment(self):
        repo = self._repo()
        sink = ws.ListLeaseSink()
        h = ws.create_worktree(repo, self.base_sha, pool_root=str(self.pool),
                               job_id="job-1", attempt_id="att-1",
                               executor_id="exec-1", sink=sink)
        # still held → conflict (ch2 writer lease)
        with self.assertRaises(WorkspaceError) as cm:
            sink.record_lease(dict(h.lease_row))
        self.assertEqual(cm.exception.reason_code, "WORKSPACE_LEASE_HELD")
        # existing dir never overwritten
        with self.assertRaises(WorkspaceError) as cm:
            ws.create_worktree(repo, self.base_sha, pool_root=str(self.pool),
                               job_id="job-1", attempt_id="att-1",
                               executor_id="exec-1", sink=sink)
        self.assertEqual(cm.exception.reason_code, "WORKSPACE_EXISTS")
        # release, remove, recreate → fencing token increments
        git(self.repo, "worktree", "remove", "--force", h.canonical_path)
        h.release_lease()
        git(self.repo, "worktree", "prune")
        h2 = ws.create_worktree(repo, self.base_sha,
                                pool_root=str(self.pool), job_id="job-1",
                                attempt_id="att-1", executor_id="exec-1",
                                sink=sink)
        self.assertEqual(h2.lease_row["fencing_token"], 2)
        git(self.repo, "worktree", "remove", "--force", h2.canonical_path)
        h2.release_lease()


class TestPwdProbe(WtTestCase):
    def _handle(self):
        repo = ws.verify_repository(str(self.repo))
        return ws.create_worktree(repo, self.base_sha,
                                  pool_root=str(self.pool), job_id="job-1",
                                  attempt_id="att-1", executor_id="exec-1",
                                  sink=ws.ListLeaseSink())

    def test_cwd_pwd_must_both_match_worktree(self):
        h = self._handle()
        try:
            cwd, env = ws.exec_plan(h)
            self.assertEqual(env["PWD"], h.canonical_path)
            old = os.getcwd()
            try:
                os.chdir(cwd)
                # consistent: cwd=PWD=worktree → probe passes
                ws.verify_execution_location(h, pwd=env["PWD"])
                # PWD changed but real cwd left behind → mismatch (ch2)
                with self.assertRaises(WorkspaceError) as cm:
                    ws.verify_execution_location(h, pwd=str(self.repo))
                self.assertEqual(cm.exception.reason_code, CONTRACT_MISMATCH)
                # backend session location mismatch → CONTRACT_MISMATCH
                with self.assertRaises(WorkspaceError) as cm:
                    ws.verify_execution_location(
                        h, pwd=env["PWD"],
                        backend_location=str(self.repo))
                self.assertEqual(cm.exception.reason_code, CONTRACT_MISMATCH)
            finally:
                os.chdir(old)
            # actual cwd in the ORIGINAL repo (PWD alone pointing at wt)
            with self.assertRaises(WorkspaceError) as cm:
                ws.verify_execution_location(h, cwd=str(self.repo),
                                             pwd=h.canonical_path)
            self.assertEqual(cm.exception.reason_code, CONTRACT_MISMATCH)
        finally:
            git(self.repo, "worktree", "remove", "--force", h.canonical_path)
            h.release_lease()
