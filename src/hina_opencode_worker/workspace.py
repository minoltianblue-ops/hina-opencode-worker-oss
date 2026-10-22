"""Repository verify, dirty-input allowlist, worktree lease, PWD probe.

WORKSPACE.md v0.3:
- ch1 verify (realpath / --show-toplevel / --git-common-dir, read-only
  status+diff capture, original index/config untouched, no stash/reset/
  implicit commit; WORKSPACE_DIRTY unless use_committed_only; explicit
  context_files allowlist; WORKSPACE_CHANGED on concurrent edit)
- ch2 create/lease/PWD (durable intent → detached worktree under
  ``<pool>/<job_id>/<executor_id>``, never overwrite, writer lease with
  fencing token + git-common-dir control record, cwd=PWD=canonical,
  CONTRACT_MISMATCH before any write)
- ch3 dirty input / new files (combined staged+unstaged working state for
  selected paths → patch; untracked raw bytes via allowlist; symlink links
  recorded not dereferenced; unsafe paths/case-collision/NUL refused;
  binary as bytes+hash; gitlink report-only CAPABILITY_UNVERIFIED;
  snapshot hash fixed BEFORE processing, drift aborts)
- ch4 entry point `WorkspaceHandle.collect` (delegates to artifacts).

The store schema (schema.sql `workspaces` row) is P1-owned and FROZEN:
this module only produces schema-shaped rows through an injected sink
(default: INSERT-only sqlite sink). No DDL here. Stdlib only. Python 3.12.
"""
from __future__ import annotations

import os
import re
import stat as _stat
import subprocess
import time
from dataclasses import dataclass, field

from . import artifacts as art
from .artifacts import ArtifactError, git, sha256_bytes

# Reason codes: WORKSPACE_DIRTY / WORKSPACE_CHANGED / CONTRACT_MISMATCH /
# CAPABILITY_UNVERIFIED are fixed by WORKSPACE.md; the rest are P3-proposed
# (recorded in HANDOFF for reviewer sign-off).
WORKSPACE_DIRTY = "WORKSPACE_DIRTY"
WORKSPACE_CHANGED = "WORKSPACE_CHANGED"
CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
CAPABILITY_UNVERIFIED = "CAPABILITY_UNVERIFIED"
REASON_REFUSED = "WORKSPACE_REFUSED"
REASON_UNSAFE_PATH = "WORKSPACE_UNSAFE_PATH"
REASON_CASE_COLLISION = "WORKSPACE_CASE_COLLISION"
REASON_EXISTS = "WORKSPACE_EXISTS"
REASON_LEASE_HELD = "WORKSPACE_LEASE_HELD"
REASON_INVALID_POLICY = "WORKSPACE_INVALID_POLICY"
REASON_REF_INVALID = "WORKSPACE_REF_INVALID"
REASON_APPLY = "WORKSPACE_APPLY_CHECK_FAILED"
REASON_INPUT_DRIFT = "WORKSPACE_INPUT_SNAPSHOT_DRIFT"


class WorkspaceError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason_code = reason


# --------------------------------------------------------------------------
# ch1 — repository / ref verification
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoInfo:
    path: str          # realpath of the actual repository root
    toplevel: str      # realpath from `git rev-parse --show-toplevel`
    git_dir: str
    common_dir: str    # realpath of --git-common-dir (controls the lock)
    head_sha: str


def verify_repository(path: str) -> RepoInfo:
    """realpath + --show-toplevel + --git-common-dir (ch1). A path prefix
    is never enough: the realpath of the given dir must EQUAL toplevel."""
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise WorkspaceError(REASON_REFUSED, f"not a directory: {path}")
    try:
        top = git(real, "rev-parse", "--show-toplevel").stdout.decode().strip()
    except ArtifactError as exc:
        raise WorkspaceError(REASON_REFUSED, f"not a git repo: {path}") from exc
    top = os.path.realpath(top)
    if top != real:
        raise WorkspaceError(
            REASON_REFUSED,
            f"path is inside repo {top} but not the root; root must be given "
            f"exactly (no prefix guessing)")
    git_dir = os.path.realpath(_abs_git_arg(real, "--absolute-git-dir"))
    common = os.path.realpath(_abs_git_arg(real, "--git-common-dir"))
    head = git(real, "rev-parse", "HEAD").stdout.decode().strip()
    return RepoInfo(path=real, toplevel=top, git_dir=git_dir,
                    common_dir=common, head_sha=head)


def _abs_git_arg(repo: str, flag: str) -> str:
    p = git(repo, "rev-parse", "--path-format=absolute", flag, check=False)
    if p.returncode == 0:
        return p.stdout.decode().strip()
    val = git(repo, "rev-parse", flag).stdout.decode().strip()
    return val if os.path.isabs(val) else os.path.join(repo, val)


_SHA_RE = re.compile(r"[0-9a-fA-F]{40}([0-9a-fA-F]{24})?\Z")
_eoos: bool | None = None  # cached --end-of-options support probe


def end_of_options_supported(repo: str) -> bool:
    global _eoos
    if _eoos is None:
        p = git(repo, "rev-parse", "--end-of-options", "--verify", "HEAD",
                check=False)
        out = p.stdout.decode().strip()
        _eoos = p.returncode == 0 and bool(_SHA_RE.match(out))
    return _eoos


def resolve_ref(repo: RepoInfo, ref: str) -> str:
    """Pin source_ref → commit SHA (ch1). Refused before exec when the ref
    could read as an option; --end-of-options used when supported, with
    manual validation as the fallback for older git."""
    if not ref or ref.strip() != ref or "\0" in ref:
        raise WorkspaceError(REASON_REF_INVALID, "empty/whitespace/NUL ref")
    if ref.startswith("-") or ref.startswith("+"):
        raise WorkspaceError(REASON_REF_INVALID,
                             f"option-injection-looking ref refused: {ref!r}")
    args = ["rev-parse", "--verify"]
    if end_of_options_supported(repo.path):
        args.append("--end-of-options")
    args.append(f"{ref}^{{commit}}")
    p = git(repo.path, *args, check=False)
    sha = p.stdout.decode().strip()
    if p.returncode != 0 or not _SHA_RE.match(sha):
        raise WorkspaceError(REASON_REF_INVALID,
                             f"{ref!r} does not resolve to a commit")
    return sha.lower()


# --------------------------------------------------------------------------
# ch1 — read-only dirty capture
# --------------------------------------------------------------------------

def porcelain_status(repo_path: str) -> list[dict]:
    """`git status --porcelain=v1 -z -uall` read-only. GIT_OPTIONAL_LOCKS=0
    keeps the ORIGINAL index byte-identical (no opportunistic refresh)."""
    out = git(repo_path, "status", "--porcelain=v1", "-z",
              "--untracked-files=all").stdout
    parts = out.split(b"\0")
    entries: list[dict] = []
    i = 0
    while i < len(parts) and parts[i]:
        rec = parts[i].decode("utf-8", "surrogateescape")
        xy, path = rec[:2], rec[3:]
        orig = None
        if "R" in xy or "C" in xy:
            i += 1
            orig = parts[i].decode("utf-8", "surrogateescape")
        entries.append({"xy": xy, "path": path, "orig_path": orig})
        i += 1
    return entries


def dirty_manifest_sha(status_entries: list[dict]) -> str:
    return sha256_bytes(art.canonical_json(
        sorted(({"xy": e["xy"], "path": e["path"],
                 "orig_path": e["orig_path"]} for e in status_entries),
               key=lambda e: (e["path"], e["xy"]))))


# --------------------------------------------------------------------------
# ch1/ch8 — input policy + allowlist validation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DirtyInputPolicy:
    include_existing_changes: bool = False
    use_committed_only: bool = False
    context_files: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.include_existing_changes and self.use_committed_only:
            raise WorkspaceError(REASON_INVALID_POLICY,
                                 "include_existing_changes and "
                                 "use_committed_only cannot both be true (ch8)")
        if self.include_existing_changes and not self.context_files:
            raise WorkspaceError(REASON_INVALID_POLICY,
                                 "include_existing_changes=true requires an "
                                 "explicit context_files list (ch8)")
        if self.use_committed_only and self.context_files:
            raise WorkspaceError(REASON_INVALID_POLICY,
                                 "use_committed_only must not carry "
                                 "context_files (ch8)")


_GLOBISH = re.compile(r"[*?\[\]]")
_DRIVE = re.compile(r"^[A-Za-z]:")


def validate_relpath(p: str, *, seen: dict[str, str] | None = None) -> str:
    """Allowlisted relative path (ch3): refuses NUL, absolute, drive,
    backslash, `.`/`..`/empty segments, any `.git` segment, glob chars and
    case-insensitive collisions against earlier entries."""
    if not p or "\0" in p:
        raise WorkspaceError(REASON_UNSAFE_PATH, f"empty/NUL path {p!r}")
    if os.path.isabs(p) or p.startswith("\\") or _DRIVE.match(p) or "\\" in p:
        raise WorkspaceError(REASON_UNSAFE_PATH, f"absolute/drive/backslash: {p!r}")
    if _GLOBISH.search(p):
        raise WorkspaceError(REASON_UNSAFE_PATH,
                             f"glob selection unsupported (ch8): {p!r}")
    segs = p.split("/")
    for seg in segs:
        if seg in ("", ".", ".."):
            raise WorkspaceError(REASON_UNSAFE_PATH, f"traversal/empty seg: {p!r}")
        if seg == ".git":
            raise WorkspaceError(REASON_UNSAFE_PATH,
                                 f".git metadata refused: {p!r}")
    if seen is not None:
        fold = p.casefold()
        owner = seen.get(fold)
        if owner is not None and owner != p:
            raise WorkspaceError(REASON_CASE_COLLISION,
                                 f"{p!r} collides with {owner!r} under "
                                 f"case-insensitive lookup")
        seen[fold] = p
    return p


def _check_inside(root: str, abs_path: str, rel: str) -> None:
    """Parent-directory chain must canonical-resolve inside root; the FINAL
    component is NOT dereferenced so recorded symlinks (ch3) whose target
    lives outside the repo are allowed — the link itself never escapes."""
    parent = os.path.realpath(os.path.dirname(abs_path) or abs_path)
    if parent != root and not parent.startswith(root + os.sep):
        raise WorkspaceError(REASON_UNSAFE_PATH,
                             f"{rel!r} parent resolves outside root: {parent}")


# --------------------------------------------------------------------------
# ch1/ch3 — selected-input capture
# --------------------------------------------------------------------------

@dataclass
class CapturedInput:
    path: str
    kind: str                  # "tracked" | "untracked" | "symlink"
    data: bytes | None         # raw bytes (None for symlink)
    link_target: str | None
    mode: str
    size: int
    content_sha256: str


@dataclass
class InputSnapshot:
    base_sha: str
    inputs: list[CapturedInput]
    patch: bytes                        # tracked selected paths vs base
    snapshot_sha256: str                # fixed BEFORE processing (ch3)
    dirty_manifest_sha256: str          # full original status at capture
    is_dirty: bool


def prepare_inputs(repo: RepoInfo, base_sha: str,
                   policy: DirtyInputPolicy) -> InputSnapshot:
    """Read-only capture of the selected dirty inputs (ch1+ch3)."""
    policy.validate()
    status = porcelain_status(repo.path)
    manifest_sha = dirty_manifest_sha(status)
    dirty = bool(status)
    if dirty and not policy.include_existing_changes \
            and not policy.use_committed_only:
        raise WorkspaceError(
            WORKSPACE_DIRTY,
            f"original repo is dirty ({len(status)} entries); refusing to "
            f"silently drop inputs — set include_existing_changes with an "
            f"explicit context_files list or use_committed_only (ch1)")
    if not policy.include_existing_changes:
        # clean repo, or explicit committed-only choice: no dirty inputs
        return InputSnapshot(base_sha=base_sha, inputs=[], patch=b"",
                             snapshot_sha256=art.canonical_snapshot_sha256([]),
                             dirty_manifest_sha256=manifest_sha,
                             is_dirty=dirty)

    tracked_index = set(git(repo.path, "ls-files", "-z").stdout.decode(
        "utf-8", "surrogateescape").split("\0"))
    seen: dict[str, str] = {}
    inputs: list[CapturedInput] = []
    tracked_paths: list[str] = []
    for rel in policy.context_files:
        validate_relpath(rel, seen=seen)
        abs_path = os.path.join(repo.path, rel)
        _check_inside(repo.path, abs_path, rel)
        if not os.path.lexists(abs_path):
            raise WorkspaceError(REASON_REFUSED, f"context file missing: {rel!r}")
        st = os.lstat(abs_path)
        if _stat.S_ISDIR(st.st_mode):
            raise WorkspaceError(REASON_REFUSED,
                                 f"directory/glob recursion unsupported (ch8): {rel!r}")
        if _stat.S_ISLNK(st.st_mode):
            tgt = os.readlink(abs_path)  # recorded, never dereferenced (ch3)
            inputs.append(CapturedInput(
                rel, "symlink", None, tgt, "120000", len(tgt),
                sha256_bytes(os.fsencode(tgt))))
        elif rel in tracked_index:
            with open(abs_path, "rb") as f:
                data = f.read()
            mode = "100755" if st.st_mode & 0o111 else "100644"
            inputs.append(CapturedInput(rel, "tracked", data, None, mode,
                                        len(data), sha256_bytes(data)))
            tracked_paths.append(rel)
        else:
            with open(abs_path, "rb") as f:
                data = f.read()
            mode = "100755" if st.st_mode & 0o111 else "100644"
            inputs.append(CapturedInput(rel, "untracked", data, None, mode,
                                        len(data), sha256_bytes(data)))

    # combined staged+unstaged working state for tracked selections (ch3):
    # `git diff <base> --` compares base commit to the working tree, i.e.
    # the merged effect of staged and unstaged edits. Read-only.
    patch = b""
    if tracked_paths:
        p = git(repo.path, "diff", "--no-ext-diff", "--no-textconv",
                "--binary", base_sha, "--", *tracked_paths, check=False)
        if p.returncode != 0:
            raise WorkspaceError(REASON_REFUSED,
                                 f"patch capture failed: {p.stderr.decode()[:200]}")
        patch = p.stdout

    snapshot_sha = art.canonical_snapshot_sha256(
        [{"path": c.path, "type": "link" if c.kind == "symlink"
          else "binary" if c.kind != "tracked" and b"\0" in (c.data or b"")[:8000]
          else "file",
          "mode": c.mode, "size": c.size,
          "content_sha256": c.content_sha256} for c in inputs])

    # original repo must be untouched by our capture; anything that moved
    # the dirty manifest is a concurrent change (checked again at apply)
    if dirty_manifest_sha(porcelain_status(repo.path)) != manifest_sha:
        raise WorkspaceError(WORKSPACE_CHANGED,
                             "original changed during capture")
    return InputSnapshot(base_sha=base_sha, inputs=inputs, patch=patch,
                         snapshot_sha256=snapshot_sha,
                         dirty_manifest_sha256=manifest_sha, is_dirty=dirty)


# --------------------------------------------------------------------------
# ch2 — worktree creation + lease sink + PWD
# --------------------------------------------------------------------------

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _safe_id(v: str, what: str) -> str:
    if not _ID_RE.match(v) or ".." in v:
        raise WorkspaceError(REASON_REFUSED, f"unsafe {what}: {v!r}")
    return v


class ListLeaseSink:
    """In-test durable sink: notes + lease rows kept in memory."""

    def __init__(self):
        self.notes: list[tuple[str, dict]] = []
        self.leases: dict[str, dict] = {}

    def durable_note(self, kind: str, payload: dict) -> None:
        self.notes.append((kind, payload))

    def record_lease(self, row: dict) -> dict:
        prev = self.leases.get(row["canonical_path"])
        if prev and prev["lease_state"] != "released":
            raise WorkspaceError(REASON_LEASE_HELD, row["canonical_path"])
        row = dict(row)
        row["fencing_token"] = (prev["fencing_token"] + 1) if prev else 1
        self.leases[row["canonical_path"]] = row
        return row

    def update_lease(self, canonical_path: str, patch: dict) -> dict:
        row = self.leases.get(canonical_path)
        if row is None:
            raise WorkspaceError(REASON_REFUSED, f"no lease {canonical_path}")
        row.update(patch)
        row["updated_at"] = _utcnow()
        return row

    def release(self, canonical_path: str) -> dict:
        return self.update_lease(canonical_path, {"lease_state": "released"})


class SqliteLeaseSink:
    """INSERT/UPDATE-only sink for the frozen `workspaces` table."""

    COLUMNS = ("canonical_path", "job_id", "attempt_id", "executor_id",
               "git_common_dir", "base_revision", "lease_state",
               "fencing_token", "owner_pid", "owner_started_at",
               "input_manifest_artifact", "created_at", "updated_at")

    def __init__(self, conn):
        self.conn = conn
        self.notes: list[tuple[str, dict]] = []

    def durable_note(self, kind: str, payload: dict) -> None:
        # Durable job/attempt intent + control-lock record. Journal-table
        # wiring is P1's store at integration (schema is frozen; P3 adds no
        # DDL). The sink boundary is the hand-off point documented in
        # HANDOFF-P3.
        self.notes.append((kind, art.canonical_json(payload)))

    def record_lease(self, row: dict) -> dict:
        cur = self.conn.execute(
            "SELECT lease_state, fencing_token FROM workspaces WHERE "
            "canonical_path = ?", (row["canonical_path"],))
        prev = cur.fetchone()
        if prev and prev[0] != "released":
            raise WorkspaceError(REASON_LEASE_HELD, row["canonical_path"])
        row = dict(row)
        row["fencing_token"] = (prev[1] + 1) if prev else 1
        cols = ", ".join(self.COLUMNS)
        ph = ", ".join("?" for _ in self.COLUMNS)
        self.conn.execute(
            f"INSERT OR REPLACE INTO workspaces ({cols}) VALUES ({ph})",
            tuple(row[c] for c in self.COLUMNS))
        self.conn.commit()
        return row

    def update_lease(self, canonical_path: str, patch: dict) -> dict:
        sets = ", ".join(f"{k} = ?" for k in patch)
        self.conn.execute(
            f"UPDATE workspaces SET {sets}, updated_at = ? "
            "WHERE canonical_path = ?",
            (*patch.values(), _utcnow(), canonical_path))
        self.conn.commit()
        cur = self.conn.execute(
            "SELECT * FROM workspaces WHERE canonical_path = ?",
            (canonical_path,))
        r = cur.fetchone()
        if r is None:
            raise WorkspaceError(REASON_REFUSED, f"no lease {canonical_path}")
        return dict(r)

    def release(self, canonical_path: str) -> dict:
        return self.update_lease(canonical_path, {"lease_state": "released"})


def _owner_started_at(pid: int) -> str | None:
    try:
        p = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class WorkspaceHandle:
    repo: RepoInfo
    base_sha: str
    job_id: str
    attempt_id: str
    executor_id: str
    canonical_path: str
    lease_row: dict
    sink: object
    input_snapshot: InputSnapshot | None = None
    baseline_manifest: dict | None = field(default=None, repr=False)
    baseline_sha: str | None = None
    released: bool = False

    # -- ch4 ------------------------------------------------------------------
    def collect(self, *, expected_ignored: tuple[str, ...] = ()) -> dict:
        if self.released:
            raise WorkspaceError(REASON_REFUSED, "lease released")
        if self.baseline_manifest is None or self.baseline_sha is None:
            raise WorkspaceError(REASON_REFUSED,
                                 "no baseline captured (apply_inputs first)")
        return art.collect_changes(self.canonical_path, self.base_sha,
                                   baseline=self.baseline_manifest,
                                   baseline_sha=self.baseline_sha,
                                   expected_ignored=expected_ignored)

    def release_lease(self) -> dict:
        row = self.sink.release(self.canonical_path)
        self.released = True
        return row


def create_worktree(repo: RepoInfo, base_sha: str, *, pool_root: str,
                    job_id: str, attempt_id: str, executor_id: str,
                    sink) -> WorkspaceHandle:
    """ch2: durable intent BEFORE creation; detached worktree under
    <pool>/<job_id>/<executor_id>; existing dir never overwritten; writer
    lease + git-common-dir control recorded."""
    _safe_id(job_id, "job_id")
    _safe_id(executor_id, "executor_id")
    attempt_id = attempt_id or f"{job_id}-1"
    pool = os.path.realpath(pool_root)
    os.makedirs(pool, mode=0o700, exist_ok=True)
    new_path = os.path.join(pool, job_id, executor_id)
    if os.path.lexists(new_path):
        raise WorkspaceError(REASON_EXISTS,
                             f"refusing to overwrite existing dir: {new_path}")
    if not _SHA_RE.match(base_sha):
        raise WorkspaceError(REASON_REF_INVALID, f"base sha {base_sha!r}")
    chk = git(repo.path, "cat-file", "-e", f"{base_sha}^{{commit}}",
              check=False)
    if chk.returncode != 0:
        raise WorkspaceError(REASON_REF_INVALID,
                             f"base sha not a commit in repo: {base_sha}")

    # durable job/attempt intent first (ch2) — creation only after it lands
    sink.durable_note("attempt_intent", {
        "job_id": job_id, "attempt_id": attempt_id,
        "executor_id": executor_id, "repository": repo.path,
        "git_common_dir": repo.common_dir, "base_sha": base_sha,
        "worktree": new_path})

    os.makedirs(os.path.dirname(new_path), mode=0o700, exist_ok=True)
    # every argument separate (ch2); no shell, no joined strings
    p = git(repo.path, "worktree", "add", "--detach", new_path, base_sha,
            check=False)
    if p.returncode != 0:
        raise WorkspaceError(
            REASON_REFUSED,
            f"worktree add failed (original repo left untouched): "
            f"{p.stderr.decode('utf-8', 'replace').strip()[:300]}")
    canonical = os.path.realpath(new_path)
    top = os.path.realpath(git(canonical, "rev-parse",
                               "--show-toplevel").stdout.decode().strip())
    common = os.path.realpath(_abs_git_arg(canonical, "--git-common-dir"))
    if top != canonical or common != repo.common_dir:
        raise WorkspaceError(CONTRACT_MISMATCH,
                             f"created worktree top={top} common={common}")

    now = _utcnow()
    lease = {
        "canonical_path": canonical, "job_id": job_id,
        "attempt_id": attempt_id, "executor_id": executor_id,
        "git_common_dir": repo.common_dir, "base_revision": base_sha,
        "lease_state": "held", "fencing_token": 1,  # sink assigns
        "owner_pid": os.getpid(), "owner_started_at": _owner_started_at(
            os.getpid()),
        "input_manifest_artifact": None,
        "created_at": now, "updated_at": now,
    }
    row = sink.record_lease(lease)
    sink.durable_note("control_lock", {  # common-dir control record (ch2)
        "git_common_dir": repo.common_dir, "job_id": job_id,
        "attempt_id": attempt_id, "canonical_path": canonical})
    handle = WorkspaceHandle(repo=repo, base_sha=base_sha, job_id=job_id,
                             attempt_id=attempt_id, executor_id=executor_id,
                             canonical_path=canonical, lease_row=row,
                             sink=sink)
    # baseline at creation; apply_inputs re-captures after inputs land so
    # collection diffs the writer's work, not the inputs (ch4)
    handle.baseline_manifest, handle.baseline_sha = art.tree_manifest(
        canonical)
    return handle


# --------------------------------------------------------------------------
# ch3 — apply selected inputs into the dedicated worktree
# --------------------------------------------------------------------------

def apply_inputs(handle: WorkspaceHandle, snapshot: InputSnapshot,
                 *, input_manifest_artifact: str | None = None) -> None:
    """`git apply --check` then apply for the tracked patch; allowlisted
    raw copies for untracked/symlink inputs; drift of the original dirty
    manifest aborts (WORKSPACE_CHANGED); final per-file sha verify."""
    if snapshot.base_sha != handle.base_sha:
        raise WorkspaceError(REASON_REFUSED,
                             "snapshot base does not match worktree base")
    # input snapshot fixed BEFORE processing; original must not have moved
    if snapshot.is_dirty or snapshot.inputs:
        cur = dirty_manifest_sha(porcelain_status(handle.repo.path))
        if cur != snapshot.dirty_manifest_sha256:
            raise WorkspaceError(
                WORKSPACE_CHANGED,
                "original dirty state changed after capture — abort before "
                "write (ch3)")

    # case-collision guard against existing worktree entries (case-insensitive
    # filesystems would silently overwrite README.md with readme.md)
    existing_fold: dict[str, str] = {}
    listing = git(handle.canonical_path, "ls-files", "-z").stdout.decode(
        "utf-8", "surrogateescape").split("\0")
    for p in listing:
        if p:
            existing_fold.setdefault(p.casefold(), p)

    if snapshot.patch:
        chk = git(handle.canonical_path, "apply", "--check", "--binary",
                  "--", input=snapshot.patch, check=False)
        if chk.returncode != 0:
            raise WorkspaceError(
                REASON_APPLY,
                f"git apply --check failed: "
                f"{chk.stderr.decode('utf-8', 'replace').strip()[:300]}")
        git(handle.canonical_path, "apply", "--binary", "--",
            input=snapshot.patch)

    for cap in snapshot.inputs:
        if cap.kind == "tracked":
            continue  # handled by the patch
        abs_path = os.path.join(handle.canonical_path, cap.path)
        _check_inside(handle.canonical_path,
                      os.path.join(handle.canonical_path,
                                   os.path.dirname(cap.path) or "."),
                      cap.path)
        owner = existing_fold.get(cap.path.casefold())
        if owner is not None and owner != cap.path:
            raise WorkspaceError(
                REASON_CASE_COLLISION,
                f"input {cap.path!r} collides with existing {owner!r} on a "
                f"case-insensitive filesystem")
        os.makedirs(os.path.dirname(abs_path), mode=0o700, exist_ok=True)
        if cap.kind == "symlink":
            if os.path.lexists(abs_path):
                raise WorkspaceError(REASON_EXISTS,
                                     f"link path already exists: {cap.path!r}")
            os.symlink(cap.link_target, abs_path)  # link itself, no deref
            existing_fold[cap.path.casefold()] = cap.path
            continue
        st = None
        try:
            st = os.lstat(abs_path)
        except FileNotFoundError:
            pass
        if st is not None:
            # patch may have created the same new file — content must match
            if not _stat_reg(st) or sha256_bytes(open(abs_path, "rb").read()) \
                    != cap.content_sha256:
                raise WorkspaceError(REASON_INPUT_DRIFT,
                                     f"existing file mismatch for {cap.path!r}")
        else:
            fd = os.open(abs_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o755 if cap.mode == "100755" else 0o644)
            try:
                os.write(fd, cap.data)
            finally:
                os.close(fd)
            existing_fold[cap.path.casefold()] = cap.path

    # verify every tracked selection landed with the captured bytes
    for cap in snapshot.inputs:
        if cap.kind != "tracked":
            continue
        abs_path = os.path.join(handle.canonical_path, cap.path)
        st = os.lstat(abs_path)
        if not _stat_reg(st) or sha256_bytes(open(abs_path, "rb").read()) \
                != cap.content_sha256:
            raise WorkspaceError(REASON_INPUT_DRIFT,
                                 f"tracked input mismatch after apply: {cap.path!r}")

    handle.input_snapshot = snapshot
    entries, sha = art.tree_manifest(handle.canonical_path)
    handle.baseline_manifest, handle.baseline_sha = entries, sha
    if input_manifest_artifact:
        handle.lease_row = handle.sink.update_lease(
            handle.canonical_path,
            {"input_manifest_artifact": input_manifest_artifact})


def _stat_reg(st) -> bool:
    return _stat.S_ISREG(st.st_mode)


# --------------------------------------------------------------------------
# ch2 — execution cwd / PWD probe (before first write)
# --------------------------------------------------------------------------

def exec_plan(handle: WorkspaceHandle, env: dict | None = None) \
        -> tuple[str, dict]:
    """cwd=PWD=canonical worktree — never one without the other (ch2)."""
    plan_env = dict(os.environ if env is None else env)
    plan_env["PWD"] = handle.canonical_path
    return handle.canonical_path, plan_env


def verify_execution_location(handle: WorkspaceHandle, *,
                              cwd: str | None = None,
                              pwd: str | None = None,
                              backend_location: str | None = None) -> None:
    """Read/pwd probe before ANY write (ch2). Mismatch → CONTRACT_MISMATCH."""
    canonical = handle.canonical_path
    real_cwd = os.path.realpath(os.getcwd() if cwd is None else cwd)
    real_pwd = os.environ.get("PWD") if pwd is None else pwd
    if real_pwd is not None:
        real_pwd = os.path.realpath(real_pwd)
    if real_cwd != canonical:
        raise WorkspaceError(CONTRACT_MISMATCH,
                             f"actual cwd {real_cwd!r} != worktree")
    if real_pwd is not None and real_pwd != canonical:
        raise WorkspaceError(CONTRACT_MISMATCH,
                             f"PWD {real_pwd!r} != actual cwd/worktree — "
                             f"changing PWD alone (or cwd alone) is refused")
    top = os.path.realpath(git(real_cwd, "rev-parse", "--show-toplevel")
                           .stdout.decode().strip())
    if top != canonical:
        raise WorkspaceError(CONTRACT_MISMATCH,
                             f"git toplevel at cwd is {top!r}, not the job "
                             f"worktree (original repo leak)")
    if backend_location is not None and \
            os.path.realpath(backend_location) != canonical:
        raise WorkspaceError(CONTRACT_MISMATCH,
                             f"backend session location "
                             f"{backend_location!r} != worktree")


# --------------------------------------------------------------------------
# ch7 (M2-Q1) — non-git seed capability. Default FALSE; a per-task profile
# is the ONLY way it turns on. The refusal marks the capability as not
# enabled for this task (WORKSPACE.md ch7: capability=false because the
# workspace feature was not implemented — never a data-category ban, D6).
# Reason codes below are P5-proposed (recorded in HANDOFF-Q1 for reviewer
# sign-off); CAPABILITY_UNVERIFIED and WORKSPACE_CHANGED are fixed codes.
# --------------------------------------------------------------------------

SEED_ORIGINAL_IS_GIT = "SEED_ORIGINAL_IS_GIT"
SEED_UNDECLARED_INPUT = "SEED_UNDECLARED_INPUT"
SEED_REPRO_MISMATCH = "SEED_REPRO_MISMATCH"


@dataclass(frozen=True)
class SeedCapability:
    non_git: bool = False  # ch7 default; per-task profile opts in explicitly

    @classmethod
    def from_task_profile(cls, profile: dict | None) -> "SeedCapability":
        """Enabled ONLY by an explicit boolean true on the task profile —
        never inferred from the repo kind, past runs, or the mere absence
        of git."""
        if not isinstance(profile, dict):
            return cls()
        return cls(non_git=profile.get("non_git") is True)


def require_seed_capability(cap: SeedCapability | None, *,
                            what: str = "non-git seed workspace") -> None:
    """Fail-closed gate: None/default keeps the M1 behaviour (false)."""
    if cap is None or not cap.non_git:
        raise WorkspaceError(
            CAPABILITY_UNVERIFIED,
            f"{what} capability=false — not enabled for this task profile "
            f"(ch7: capability marks the workspace feature as unimplemented"
            f"/un-enabled for this task, not a ban on the data kind)")
