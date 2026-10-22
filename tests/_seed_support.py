"""M2-Q1 seed-flow test support: non-git original dirs, seed WTs, residual 0.

One temp root per test (original + seeds + pools); teardown removes seed
worktrees, releases leases, deletes the root and asserts nothing is left.
Git stays isolated from the user's config (identity via env only).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests._wt_support import git, write  # noqa: F401 (write re-exported)


# standard non-git original used by U1–U3
SPEC = b"spec v1\n"
CSV = b"a,1\nb,2\n"
SH = b"#!/bin/sh\necho hi\n"
README = b"top readme\n"
CACHE = b"\x00\x01cached\xff"
SECRET = b"TOKEN=declared-excluded\n"

DEFAULT_FILES = ("notes/spec.md", "data/input.csv", "scripts/run.sh",
                 "docs/Readme.md", "latest")
DEFAULT_EXCLUSIONS = ("cache", "secret.env")


def build_default_original(original: Path) -> None:
    """Non-git scratch materials: regular files (one exec), a relative
    symlink, an excluded dir, an excluded file."""
    write(original / "notes" / "spec.md", SPEC)
    write(original / "data" / "input.csv", CSV)
    write(original / "scripts" / "run.sh", SH)
    os.chmod(original / "scripts" / "run.sh", 0o755)
    write(original / "docs" / "Readme.md", README)
    (original / "latest").symlink_to(os.path.join("data", "input.csv"))
    write(original / "cache" / "blob.bin", CACHE)
    write(original / "secret.env", SECRET)


def default_policy():
    from hina_opencode_worker.seedrun import SeedInputPolicy
    return SeedInputPolicy(files=DEFAULT_FILES,
                           exclusions=DEFAULT_EXCLUSIONS)


def enabled():
    from hina_opencode_worker.workspace import SeedCapability
    return SeedCapability(non_git=True)


class SeedTestCase(unittest.TestCase):
    """Temp root + non-git original + seed pool; zero residual on teardown."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hina-seed-")
        self.tmp = Path(self._tmp.name)
        self.original = self.tmp / "original"
        self.original.mkdir()
        build_default_original(self.original)
        self.pool = self.tmp / "pool"
        self._seed_wts = []  # (seed_repo_path, handle)

    def track_seed_wt(self, handle):
        self._seed_wts.append((str(handle.repo.path), handle))
        return handle

    def tearDown(self):
        for repo_path, h in self._seed_wts:
            try:
                git(repo_path, "worktree", "remove", "--force",
                    h.canonical_path, check=False)
                h.release_lease()
            except Exception:
                pass
        self._tmp.cleanup()
        self.assertFalse(self.tmp.exists(),
                         f"residual files left in {self.tmp}")
