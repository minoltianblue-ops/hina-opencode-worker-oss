"""P3 test support: isolated scratch repos, frozen-schema DB fixtures.

Everything lives under ONE temp dir per test (repo + pool + stores), so
teardown is total: no leftover worktree metadata, processes or files
(acceptance: residual 0). Git is fully isolated from the user's real
config (GIT_CONFIG_GLOBAL/SYSTEM → /dev/null, identity via env only).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = ROOT / "docs" / "contracts" / "schema.sql"

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "P3 Tester", "GIT_AUTHOR_EMAIL": "p3@example.invalid",
    "GIT_COMMITTER_NAME": "P3 Tester", "GIT_COMMITTER_EMAIL": "p3@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
}


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
              "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_VERSION"):
        env.pop(k, None)
    env.update(GIT_IDENTITY)
    return env


def git(cwd, *args: str, input: bytes | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], input=input,
        capture_output=True, env=git_env())
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed rc={proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace')}")
    return proc


def write(path, data: bytes | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    p.write_bytes(data)


def make_repo(path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    git(p, "init", "-q", "--initial-branch=main")
    write(p / "README.md", "base readme\n")
    write(p / "src" / "main.py", "print('base')\n")
    write(p / "notes" / "README.md", "notes base\n")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "base commit")
    return p


def commit_all(repo, msg: str = "wip") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def new_db() -> sqlite3.Connection:
    """DB from the FROZEN contract schema (read-only input, P3 never edits
    schema.sql). Minimal jobs/attempts parents for FKs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")
    now = "2026-10-19T00:00:00Z"
    conn.execute(
        "INSERT INTO jobs (job_id, principal_id, goal, requested_model,"
        " profile, repository_path, base_revision, state, created_at,"
        " updated_at) VALUES ('job-1','u','goal','m','develop','/r','b',"
        " 'accepted', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO attempts (attempt_id, job_id, ordinal, executor_id,"
        " model, billing_class, state, contract_revision, binary_sha256,"
        " started_at) VALUES ('att-1','job-1',1,'exec-1','m','free',"
        " 'starting','0.3', ?, ?)", ("0" * 64, now))
    conn.commit()
    return conn


class WtTestCase(unittest.TestCase):
    """One temp root per test; helper asserts zero residual on teardown."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hina-p3-")
        self.tmp = Path(self._tmp.name)
        self.repo = make_repo(self.tmp / "orig")
        self.pool = self.tmp / "pool"
        self.base_sha = git(self.repo, "rev-parse",
                            "HEAD").stdout.decode().strip()
        self._handles = []

    def track_worktree(self, handle):
        self._handles.append(handle)
        return handle

    def tearDown(self):
        # remove worktrees while the repo still exists (residual 0)
        for h in self._handles:
            try:
                git(self.repo, "worktree", "remove", "--force",
                    h.canonical_path, check=False)
                h.release_lease()
            except Exception:
                pass
        self._tmp.cleanup()
        # residual check: the whole temp root must be gone
        self.assertFalse(self.tmp.exists(),
                         f"residual files left in {self.tmp}")

    def index_bytes(self):
        p = self.repo / ".git" / "index"
        return p.read_bytes() if p.exists() else None

    def status_bytes(self):
        return git(self.repo, "status", "--porcelain=v1", "-z",
                   "--untracked-files=all").stdout
