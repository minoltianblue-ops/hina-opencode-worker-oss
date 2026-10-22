"""M2-Q1 seed runner — non-git selected materials → scratch seed repository.

WORKSPACE.md ch7: non-git materials are never edited in place. Only the
SELECTED files are copied into a dedicated seed repository; worker AND
reviewer worktrees are created on that seed; the worker's tracked patch is
applied back to the seed behind a `git apply --check` gate; untracked new
files are collected as raw bytes + sha (ch4); and the ORIGINAL must be
provably untouched — a full hash manifest of the original is compared
before and after the whole flow (drift → WORKSPACE_CHANGED).

The non-git capability defaults to FALSE (``SeedCapability`` in
workspace.py) and is enabled per-task-profile only; the refusal reason is
CAPABILITY_UNVERIFIED — the feature not being enabled for this task, never
a data-category ban (D6). Exclusions must be DECLARED: an original file
that is neither selected nor covered by a declared exclusion aborts with
SEED_UNDECLARED_INPUT instead of being silently skipped (ch3).

Extend, do not break: reuses the frozen ch2 worktree/lease machinery
(`ws.create_worktree`), ch3 path validation (`ws.validate_relpath`), ch3
input application (`ws.apply_inputs`), ch4 collection
(`WorkspaceHandle.collect`) and ch5 reviewer-repro primitives
(`art.tree_manifest` + `art.snapshot_diff`). Stdlib only. Python 3.12.
"""
from __future__ import annotations

import json
import os
import stat as _stat
import subprocess
from dataclasses import dataclass, field

from . import artifacts as art
from . import workspace as ws
from .artifacts import sha256_bytes
from .workspace import (REASON_APPLY, REASON_CASE_COLLISION,
                        REASON_EXISTS, REASON_INPUT_DRIFT,
                        REASON_INVALID_POLICY, REASON_REFUSED,
                        REASON_REF_INVALID, REASON_UNSAFE_PATH,
                        SEED_ORIGINAL_IS_GIT, SEED_REPRO_MISMATCH,
                        SEED_UNDECLARED_INPUT, WORKSPACE_CHANGED,
                        SeedCapability, WorkspaceError, validate_relpath)

# Deterministic seed base commit: same selected bytes + modes ⇒ same seed
# base SHA on any host/reviewer (tree identity proof for U2).
SEED_IDENTITY = ("hina-seed", "seed@hina.local.invalid")
SEED_EPOCH = "2000-01-01T00:00:00 +0000"


def _seed_git(cwd, *args: str, input: bytes | None = None,
              check: bool = True) -> subprocess.CompletedProcess:
    """Isolated git (artifacts.git env rules) + fixed identity/date so the
    seed base commit is content-deterministic. Every argument separate."""
    env = art.git_env(GIT_AUTHOR_DATE=SEED_EPOCH, GIT_COMMITTER_DATE=SEED_EPOCH)
    p = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", f"user.name={SEED_IDENTITY[0]}",
         "-c", f"user.email={SEED_IDENTITY[1]}",
         "-C", str(cwd), *args],
        input=input, capture_output=True, env=env)
    if check and p.returncode != 0:
        raise WorkspaceError(
            REASON_REFUSED,
            f"seed git {' '.join(args)} rc={p.returncode}: "
            f"{p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return p


# --------------------------------------------------------------------------
# original hash manifest — the untouched-original proof (before/after)
# --------------------------------------------------------------------------

def original_hash_manifest(original) -> dict[str, dict]:
    """Read-only recursive manifest of a NON-GIT original directory.

    Every regular file is hashed from raw bytes; symlinks are recorded as
    links (target hashed, never dereferenced); symlinked directories are
    recorded as dirlink entries so they cannot be silently skipped; plain
    directories carry no data and need no declaration. A git repo root is
    refused — that is the ch1 worktree flow's job, not the seed flow's.
    """
    root = os.path.realpath(str(original))
    if not os.path.isdir(root):
        raise WorkspaceError(REASON_REFUSED, f"not a directory: {original}")
    if os.path.lexists(os.path.join(root, ".git")):
        raise WorkspaceError(
            SEED_ORIGINAL_IS_GIT,
            "original is a git repository — use the git worktree flow "
            "(ch1); the seed flow is for non-git materials")
    out: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        for name in dirnames:
            abs_dir = os.path.join(dirpath, name)
            if _stat.S_ISLNK(os.lstat(abs_dir).st_mode):
                # names come from the directory listing itself (no
                # traversal possible); enforcement happens at selection,
                # exclusion and copy time — never by skipping here
                rel = _rel(rel_dir, name)
                tgt = os.readlink(abs_dir)  # recorded, never followed
                out[rel] = {"type": "dirlink", "target": tgt,
                            "size": len(tgt),
                            "content_sha256": sha256_bytes(os.fsencode(tgt)),
                            "mode": "120000"}
        for name in filenames:
            rel = _rel(rel_dir, name)
            abs_path = os.path.join(dirpath, name)
            st = os.lstat(abs_path)
            if _stat.S_ISLNK(st.st_mode):
                tgt = os.readlink(abs_path)
                out[rel] = {"type": "link", "target": tgt,
                            "size": len(tgt),
                            "content_sha256": sha256_bytes(os.fsencode(tgt)),
                            "mode": "120000"}
            elif _stat.S_ISREG(st.st_mode):
                with open(abs_path, "rb") as f:
                    data = f.read()
                out[rel] = {"type": "file", "target": None, "size": len(data),
                            "content_sha256": sha256_bytes(data),
                            "mode": "100755" if st.st_mode & 0o111
                            else "100644"}
            else:  # fifo/socket/device — never copied; must be declared
                out[rel] = {
                    "type": "special", "target": None, "size": 0,
                    "content_sha256": sha256_bytes(
                        f"special:{_stat.S_IFMT(st.st_mode)}".encode()),
                    "mode": str(_stat.S_IFMT(st.st_mode))}
    return out


def original_manifest_sha256(manifest: dict[str, dict]) -> str:
    return art.canonical_snapshot_sha256(
        [{"path": p, "type": e["type"], "mode": e["mode"],
          "size": e["size"], "content_sha256": e["content_sha256"]}
         for p, e in manifest.items()])


def verify_original_untouched(original, before: dict[str, dict]) -> None:
    """Re-hash the original and compare against the pre-flow manifest.
    Any added/removed/changed entry — including a .git appearing — means
    the original moved: WORKSPACE_CHANGED (ch1/ch3 semantics)."""
    try:
        after = original_hash_manifest(original)
    except WorkspaceError as exc:
        raise WorkspaceError(
            WORKSPACE_CHANGED,
            f"original changed after capture: {exc}") from exc
    if original_manifest_sha256(after) != original_manifest_sha256(before):
        added = sorted(p for p in after if p not in before)
        removed = sorted(p for p in before if p not in after)
        changed = sorted(p for p in after
                         if p in before and
                         after[p]["content_sha256"] !=
                         before[p]["content_sha256"])
        raise WorkspaceError(
            WORKSPACE_CHANGED,
            f"original hash manifest drifted: added={added[:5]} "
            f"removed={removed[:5]} changed={changed[:5]}")


def _rel(rel_dir: str, name: str) -> str:
    rd = rel_dir.replace(os.sep, "/")
    return name if rd == os.curdir else f"{rd}/{name}"


# --------------------------------------------------------------------------
# selection policy — files + DECLARED exclusions, never silent skips
# --------------------------------------------------------------------------

def _validate_exclusion(p: str, *, seen: dict[str, str]) -> str:
    """Exclusion declarations follow validate_relpath's traversal/absolute
    rules but are LITERAL prefix strings, never patterns: they may name a
    `.git` segment (declaring it never-copied is the point) and literal
    glob-looking characters (no expansion ever happens), so every original
    entry stays declarable and can never be silently skipped."""
    if not p or "\0" in p:
        raise WorkspaceError(REASON_UNSAFE_PATH, f"empty/NUL exclusion {p!r}")
    if os.path.isabs(p) or p.startswith("\\") or "\\" in p:
        raise WorkspaceError(REASON_UNSAFE_PATH,
                             f"absolute/backslash exclusion: {p!r}")
    for seg in p.split("/"):
        if seg in ("", ".", ".."):
            raise WorkspaceError(REASON_UNSAFE_PATH,
                                 f"traversal/empty seg: {p!r}")
    fold = p.casefold()
    owner = seen.get(fold)
    if owner is not None and owner != p:
        raise WorkspaceError(REASON_CASE_COLLISION,
                             f"exclusion {p!r} collides with {owner!r} "
                             f"under case-insensitive lookup")
    seen[fold] = p
    return p


@dataclass(frozen=True)
class SeedInputPolicy:
    files: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    def validate(self) -> None:
        seen: dict[str, str] = {}
        for f in self.files:
            validate_relpath(f, seen=seen)
        for x in self.exclusions:
            _validate_exclusion(x, seen=seen)
        # a file may not also sit under an exclusion (contradictory intent)
        for f in self.files:
            fold = f.casefold()
            for x in self.exclusions:
                xs = x.casefold()
                if fold == xs or fold.startswith(xs + "/"):
                    raise WorkspaceError(
                        REASON_INVALID_POLICY,
                        f"{f!r} is selected AND covered by exclusion {x!r}")


def _covers(exclusion: str, path: str) -> bool:
    e, p = exclusion.casefold(), path.casefold()
    return p == e or p.startswith(e + "/")


# --------------------------------------------------------------------------
# seed creation
# --------------------------------------------------------------------------

@dataclass
class SeedResult:
    original: str
    original_manifest: dict[str, dict]
    original_manifest_sha256: str
    seed_path: str
    seed_base_sha: str
    seed_tree_sha256: str
    copied: list[dict] = field(default_factory=list)
    exclusion_report: list[dict] = field(default_factory=list)


def create_seed_repo(original, policy: SeedInputPolicy, *, seed_path,
                     capability: SeedCapability | None,
                     sink=None) -> SeedResult:
    """ch7 bootstrap: copy ONLY selected materials into a fresh scratch git
    repo, deterministic base commit, exclusions declared and never copied,
    originals proven untouched (before/after hash compare)."""
    ws.require_seed_capability(capability)
    policy.validate()

    orig_root = os.path.realpath(str(original))
    before = original_hash_manifest(orig_root)
    seed = os.path.realpath(str(seed_path))
    if seed == orig_root or seed.startswith(orig_root + os.sep):
        raise WorkspaceError(
            REASON_UNSAFE_PATH,
            f"seed path {seed!r} is inside the original {orig_root!r} — "
            f"the seed must live OUTSIDE the original (untouched rule)")
    if os.path.lexists(seed):
        raise WorkspaceError(REASON_EXISTS,
                             f"refusing to overwrite existing dir: {seed}")

    manifest = before
    missing = sorted(f for f in policy.files if f not in manifest)
    if missing:
        raise WorkspaceError(REASON_REFUSED,
                             f"selected material missing in original: "
                             f"{missing[:5]}")

    # disposition pass — EVERY original entry is copied, declared-excluded,
    # or the flow aborts (never silently skipped, ch3).
    selected = set(policy.files)
    undeclared = sorted(
        p for p in manifest
        if p not in selected
        and not any(_covers(x, p) for x in policy.exclusions))
    if undeclared:
        raise WorkspaceError(
            SEED_UNDECLARED_INPUT,
            f"{len(undeclared)} original file(s) neither selected nor "
            f"declared-excluded (never silently skipped): "
            f"{undeclared[:8]}")

    exclusion_report = []
    for x in policy.exclusions:
        matched = sorted(p for p in manifest if _covers(x, p))
        if not matched:
            raise WorkspaceError(
                REASON_INVALID_POLICY,
                f"exclusion {x!r} covers nothing in the original — stale or "
                f"wrong declaration (exclusions must be real)")
        kind = "file" if x in manifest else "dir"
        exclusion_report.append({"declaration": x, "kind": kind,
                                 "matched": len(matched)})

    if sink is not None:
        sink.durable_note("seed_intent", {
            "original": orig_root,
            "original_manifest_sha256": original_manifest_sha256(manifest),
            "seed_path": seed, "files": sorted(policy.files),
            "exclusions": sorted(policy.exclusions)})

    os.makedirs(os.path.dirname(seed), mode=0o700, exist_ok=True)
    os.makedirs(seed, mode=0o700)
    _seed_git(seed, "init", "-q", "--initial-branch=main")

    copied: list[dict] = []
    for rel in sorted(policy.files):
        entry = manifest[rel]
        if entry["type"] in ("special", "dirlink"):
            raise WorkspaceError(
                REASON_REFUSED,
                f"{entry['type']} {rel!r} cannot be copied into the seed — "
                f"declare it as an exclusion instead")
        abs_src = os.path.join(orig_root, rel)
        abs_dst = os.path.join(seed, rel)
        os.makedirs(os.path.dirname(abs_dst), mode=0o700, exist_ok=True)
        if entry["type"] == "link":
            # the link itself, never the (possibly external) target (ch3)
            os.symlink(entry["target"], abs_dst)
        else:
            with open(abs_src, "rb") as f:
                data = f.read()
            fd = os.open(abs_dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o755 if entry["mode"] == "100755" else 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            # copy fidelity: re-hash the bytes as written
            with open(abs_dst, "rb") as f:
                got = sha256_bytes(f.read())
            if got != entry["content_sha256"]:
                raise WorkspaceError(
                    REASON_INPUT_DRIFT,
                    f"seed copy fidelity mismatch for {rel!r}: "
                    f"{got[:16]}… != {entry['content_sha256'][:16]}…")
        copied.append({"path": rel, "type": entry["type"],
                       "mode": entry["mode"], "size": entry["size"],
                       "content_sha256": entry["content_sha256"]})

    _seed_git(seed, "add", "-A")
    _seed_git(seed, "commit", "-q", "-m",
              "seed: non-git selected materials (WORKSPACE.md ch7)")
    base_sha = _seed_git(seed, "rev-parse", "HEAD").stdout.decode().strip()
    _entries, tree_sha = art.tree_manifest(seed)

    verify_original_untouched(orig_root, before)
    return SeedResult(original=orig_root, original_manifest=before,
                      original_manifest_sha256=original_manifest_sha256(
                          before),
                      seed_path=seed, seed_base_sha=base_sha,
                      seed_tree_sha256=tree_sha, copied=copied,
                      exclusion_report=exclusion_report)


# --------------------------------------------------------------------------
# worker / reviewer worktrees ON the seed + result collection
# --------------------------------------------------------------------------

def seed_workspace(seed: SeedResult, *, pool_root: str, job_id: str,
                   attempt_id: str = "", executor_id: str = "worker",
                   sink) -> ws.WorkspaceHandle:
    """Detached worktree on the SEED repo (ch2 machinery, seed as repo)."""
    repo = ws.verify_repository(seed.seed_path)
    if repo.head_sha != seed.seed_base_sha:
        raise WorkspaceError(SEED_REPRO_MISMATCH,
                             "seed HEAD moved off its base commit")
    return ws.create_worktree(repo, seed.seed_base_sha,
                              pool_root=pool_root, job_id=job_id,
                              attempt_id=attempt_id,
                              executor_id=executor_id, sink=sink)


@dataclass
class SeedWorkResult:
    seed_base_sha: str
    patch: bytes
    new_files: list  # ws.CapturedInput (untracked / symlink)
    changes: dict    # ch4 manifest from WorkspaceHandle.collect()
    tree_sha256: str
    tree_entries: dict = field(repr=False, default_factory=dict)


def collect_worker_result(handle: ws.WorkspaceHandle) -> SeedWorkResult:
    """ch4 collection on the worker seed-WT: tracked patch + ALL new files
    as raw bytes + sha (new-file omission is a review blocker, ch5)."""
    res = handle.collect()
    patch = res["tracked_patch"]
    seen: dict[str, str] = {}
    new_files: list[ws.CapturedInput] = []
    for e in res["new_files"]:
        rel = validate_relpath(e["relative_path"], seen=seen)
        abs_path = os.path.join(handle.canonical_path, rel)
        st = os.lstat(abs_path)
        if _stat.S_ISLNK(st.st_mode):
            tgt = os.readlink(abs_path)
            new_files.append(ws.CapturedInput(
                rel, "symlink", None, tgt, "120000", len(tgt),
                sha256_bytes(os.fsencode(tgt))))
        elif _stat.S_ISREG(st.st_mode):
            with open(abs_path, "rb") as f:
                data = f.read()
            if sha256_bytes(data) != e.get("after_sha256"):
                raise WorkspaceError(
                    REASON_INPUT_DRIFT,
                    f"new file changed between collection and capture: "
                    f"{rel!r}")
            mode = "100755" if st.st_mode & 0o111 else "100644"
            new_files.append(ws.CapturedInput(rel, "untracked", data, None,
                                              mode, len(data),
                                              sha256_bytes(data)))
        else:
            raise WorkspaceError(REASON_REFUSED,
                                 f"new path {rel!r} is neither file nor "
                                 f"symlink — not collectable")
    return SeedWorkResult(seed_base_sha=handle.base_sha, patch=patch,
                          new_files=new_files, changes=res,
                          tree_sha256=res["source_snapshot_sha256"],
                          tree_entries=res["tree_manifest"])


# --------------------------------------------------------------------------
# apply-back to the seed — `git apply --check` gate, then raw new files
# --------------------------------------------------------------------------

def apply_back_to_seed(seed: SeedResult, work: SeedWorkResult, *,
                       capability: SeedCapability | None = None) -> tuple[
        dict, str]:
    """Apply the worker's result back onto the seed repo itself (scratch
    integration point; the non-git original is NEVER the apply target).
    HEAD must still be the seed base (ch6: no blind apply onto a moved
    base); tracked patch is gated by `--check` BEFORE any write; new files
    are written with per-file sha verify and case-collision guards."""
    if capability is not None:
        ws.require_seed_capability(capability)
    head = _seed_git(seed.seed_path, "rev-parse", "HEAD").stdout.decode(
        "utf-8", "surrogateescape").strip()
    if head != work.seed_base_sha:
        raise WorkspaceError(
            REASON_REF_INVALID,
            f"seed HEAD {head[:12]} != patch base "
            f"{work.seed_base_sha[:12]} — rebase/review first (ch6)")

    if work.patch:
        chk = _seed_git(seed.seed_path, "apply", "--check", "--binary", "--",
                        input=work.patch, check=False)
        if chk.returncode != 0:
            raise WorkspaceError(
                REASON_APPLY,
                f"git apply --check failed on seed: "
                f"{chk.stderr.decode('utf-8', 'replace').strip()[:300]}")
        _seed_git(seed.seed_path, "apply", "--binary", "--",
                  input=work.patch)

    existing_fold: dict[str, str] = {}
    for p in _seed_git(seed.seed_path, "ls-files", "-z").stdout.decode(
            "utf-8", "surrogateescape").split("\0"):
        if p:
            existing_fold.setdefault(p.casefold(), p)
    seen: dict[str, str] = {}
    for cap in work.new_files:
        rel = validate_relpath(cap.path, seen=seen)  # defense in depth
        owner = existing_fold.get(rel.casefold())
        if owner is not None and owner != rel:
            raise WorkspaceError(
                REASON_CASE_COLLISION,
                f"new file {rel!r} collides with seed entry {owner!r} on a "
                f"case-insensitive filesystem")
        abs_path = os.path.join(seed.seed_path, rel)
        ws._check_inside(seed.seed_path,
                         os.path.join(seed.seed_path,
                                      os.path.dirname(rel) or "."), rel)
        os.makedirs(os.path.dirname(abs_path), mode=0o700, exist_ok=True)
        if os.path.lexists(abs_path):
            if cap.kind == "symlink" and _stat.S_ISLNK(
                    os.lstat(abs_path).st_mode) \
                    and os.readlink(abs_path) == cap.link_target:
                continue  # patch-created identical link
            raise WorkspaceError(REASON_EXISTS,
                                 f"new file path already exists: {rel!r}")
        if cap.kind == "symlink":
            os.symlink(cap.link_target, abs_path)
        else:
            fd = os.open(abs_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o755 if cap.mode == "100755" else 0o644)
            try:
                os.write(fd, cap.data)
            finally:
                os.close(fd)
            with open(abs_path, "rb") as f:
                got = sha256_bytes(f.read())
            if got != cap.content_sha256:
                raise WorkspaceError(
                    REASON_INPUT_DRIFT,
                    f"new file verify failed: {rel!r}")
        existing_fold[rel.casefold()] = rel
    return art.tree_manifest(seed.seed_path)


# --------------------------------------------------------------------------
# reviewer reproduction on the seed (ch5) — second WT, exact repro
# --------------------------------------------------------------------------

def reviewer_seed_repro(original, policy: SeedInputPolicy,
                        work: SeedWorkResult, *, expected: SeedResult,
                        seed_path, pool_root: str, job_id: str,
                        sink, capability: SeedCapability | None) -> dict:
    """Independent reviewer: fresh seed copy from the SAME original+policy,
    reviewer WT on it, exact reproduction of the raw patch + ALL new files
    (ws.apply_inputs), tree compare via art.snapshot_diff — a different
    revision or a missing new file is SEED_REPRO_MISMATCH (ch5 BLOCKED).
    The original is re-verified untouched at the end."""
    seed2 = create_seed_repo(original, policy, seed_path=seed_path,
                             capability=capability, sink=sink)
    if seed2.seed_base_sha != expected.seed_base_sha:
        raise WorkspaceError(
            SEED_REPRO_MISMATCH,
            f"seed base differs: reviewer {seed2.seed_base_sha[:12]} != "
            f"writer {expected.seed_base_sha[:12]} — copy not reproducible")

    handle = seed_workspace(seed2, pool_root=pool_root,
                            job_id=f"{job_id}-review",
                            executor_id="reviewer", sink=sink)
    snap = ws.InputSnapshot(
        base_sha=seed2.seed_base_sha, inputs=list(work.new_files),
        patch=work.patch,
        snapshot_sha256=art.canonical_snapshot_sha256(
            [{"path": c.path, "type": "link" if c.kind == "symlink"
              else "file", "mode": c.mode, "size": c.size,
              "content_sha256": c.content_sha256}
             for c in work.new_files]),
        dirty_manifest_sha256=ws.dirty_manifest_sha([]), is_dirty=False)
    ws.apply_inputs(handle, snap)

    actual_entries, actual_sha = art.tree_manifest(handle.canonical_path)
    diff = art.snapshot_diff(work.tree_sha256, work.tree_entries,
                             actual_sha, actual_entries)
    if not diff["match"]:
        raise WorkspaceError(
            SEED_REPRO_MISMATCH,
            "reviewer tree does not reproduce writer result (ch5 BLOCKED): "
            + json.dumps({k: diff[k] for k in
                          ("missing", "extra", "changed")},
                         sort_keys=True)[:400])
    verify_original_untouched(original, expected.original_manifest)
    return {"match": True, "seed_base_sha": seed2.seed_base_sha,
            "reviewer_worktree": handle.canonical_path,
            "snapshot_sha256": actual_sha, "diff": diff,
            "handle": handle}
