"""Result collection, changes manifest, snapshot hashing, preview/export.

WORKSPACE.md v0.3 ch4 (result collection — never rely on a single
``git diff``), ch5 (reviewer exact repro, stale checks must not be reused).
schema.sql (frozen, read-only) defines the ``artifacts`` row shape this
module emits — this module never alters the store schema.

Invariants enforced here (contract refs in comments):
- private full artifact is stored BEFORE any page export (ch4).
- truncation is allowed only in preview; a truncated export page is never
  marked complete (ch4).
- source snapshot hash = SHA256 of sorted canonical JSON of
  {path,type,mode,size,content_sha256} over REAL bytes before processing;
  it is distinct from the exported patch hash (ch4).
- redacted export artifacts are never applyable (schema CHECK).
- cursors are signed, expiring and bound to one artifact generation:
  tampered → CURSOR_MUTATED, past TTL → CURSOR_EXPIRED, wrong/unknown
  generation → CURSOR_STALE (ch5 stale-check reuse is refused).

Stdlib only. Python 3.12.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat as statmod
import subprocess
import time
import uuid

# --- caps (T11 bounds): preview 32k, export chunk 500k, single artifact 1MiB
PREVIEW_LIMIT = 32 * 1024            # 32768
EXPORT_CHUNK_LIMIT = 500_000         # per exported page
MAX_SINGLE_EXPORT_BYTES = 1_048_576  # 1 MiB: larger exports MUST be paged
CURSOR_TTL_SECONDS = 900.0

ARTIFACT_SECTIONS = ("summary", "checks", "changes", "artifacts",
                     "content", "input", "raw")
ARTIFACT_VISIBILITIES = ("private", "export")

# Contract (MCP.md §2, frozen mcp-tools.json patterns): artifact_id is
# ``art_`` + lowercase UUIDv4. The connector-side validator rejects every
# other shape (observed rejection 2026-10-20: "art-eaa043a44cc43d25a929ffb5"
# — content-derived hex — was refused, blocking ChatGPT-side delivery).
# Row content binding stays on the row's own sha256, never on the id.
ARTIFACT_ID_RE = re.compile(
    r"^art_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$")


def new_artifact_id() -> str:
    aid = "art_" + str(uuid.uuid4())
    assert ARTIFACT_ID_RE.match(aid), aid  # emitted format is contract
    return aid

# Reason codes proposed by P3 (not fixed by contract; recorded in HANDOFF).
REASON_INVALID_ROW = "ARTIFACT_INVALID_ROW"
REASON_REGISTRY_CONFLICT = "ARTIFACT_REGISTRY_CONFLICT"
REASON_ORDER = "ARTIFACT_ORDER_VIOLATION"      # export before private store (ch4)
REASON_TRUNCATED = "ARTIFACT_TRUNCATION_COMPLETE"  # truncated marked complete
REASON_TOO_LARGE = "ARTIFACT_EXPORT_NEEDS_PAGING"
REASON_CURSOR_MUTATED = "CURSOR_MUTATED"
REASON_CURSOR_EXPIRED = "CURSOR_EXPIRED"
REASON_CURSOR_STALE = "CURSOR_STALE"
REASON_SNAPSHOT_STALE = "SNAPSHOT_CHECK_STALE"


class ArtifactError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason_code = reason


# --------------------------------------------------------------------------
# canonical JSON + snapshot hashing (WORKSPACE.md ch4)
# --------------------------------------------------------------------------

def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def canonical_snapshot_sha256(entries: list[dict]) -> str:
    """SHA256 over sorted {path,type,mode,size,content_sha256} entries."""
    required = {"path", "type", "mode", "size", "content_sha256"}
    norm = []
    for e in entries:
        missing = required - set(e)
        if missing:
            raise ArtifactError(REASON_INVALID_ROW,
                                f"snapshot entry missing {sorted(missing)}")
        norm.append({k: e[k] for k in sorted(required)})
    norm.sort(key=lambda e: e["path"])
    return hashlib.sha256(canonical_json(norm)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# tree manifest / source snapshot
# --------------------------------------------------------------------------

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def git_env(**extra: str) -> dict[str, str]:
    """Isolated env: no user/global git config, no opportunistic index
    writes (original index must stay byte-identical), no prompts."""
    env = os.environ.copy()
    for k in _GIT_ENV_DROP:
        env.pop(k, None)
    env.update({
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    })
    env.update(extra)
    return env


def git(cwd, *args: str, input: bytes | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-C", str(cwd), *args],
        input=input, capture_output=True, env=git_env(),
    )
    if check and proc.returncode != 0:
        raise ArtifactError("GIT_FAILED",
                            f"git {' '.join(args)} rc={proc.returncode} "
                            f"stderr={proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc


def _file_type_and_content(path: str) -> tuple[str, bytes, int]:
    """(type, content-bytes-hashed, size) WITHOUT dereferencing symlinks.

    content bytes are the REAL bytes for files; for links the recorded
    link-target string; gitlinks report mode 160000 upstream.
    """
    st = os.lstat(path)
    if statmod.S_ISLNK(st.st_mode):
        target = os.readlink(path)
        return "link", os.fsencode(target), len(target)
    with open(path, "rb") as f:  # regular file: raw bytes, no processing
        data = f.read()
    return "file", data, st.st_size


def _mode_for(path: str, st) -> str:
    if statmod.S_ISLNK(st.st_mode):
        return "120000"
    return "100755" if st.st_mode & statmod.S_IXUSR else "100644"


def tree_manifest(worktree) -> tuple[dict[str, dict], str]:
    """Manifest of tracked + untracked (non-ignored) files in *worktree*.

    Not "one git diff" (ch4): tracked from ``git ls-files -s`` (mode
    incl. 160000 gitlink, 120000 link) and untracked from
    ``git ls-files --others --exclude-standard -z``, each hashed from real
    bytes on disk. Returns ({path: entry}, snapshot_sha256).
    """
    entries: dict[str, dict] = {}
    tracked = git(worktree, "ls-files", "-s", "-z").stdout.split(b"\0")
    for line in tracked:
        if not line:
            continue
        meta, raw = line.split(b"\t", 1)
        mode, oid, _stage = meta.split(b" ", 2)
        path = os.fsdecode(raw)
        entries[path] = {"path": path, "tracked": True,
                         "mode": mode.decode(), "_oid": oid.decode()}
    others = git(worktree, "ls-files", "--others", "--exclude-standard",
                 "-z").stdout.split(b"\0")
    for raw in others:
        if raw:
            path = os.fsdecode(raw)
            entries[path] = {"path": path, "tracked": False, "mode": None}
    absent = []
    for path, e in entries.items():
        abs_path = os.path.join(str(worktree), path)
        if e["mode"] == "160000":  # gitlink: detect/report only (ch3)
            e.update(type="gitlink", content_sha256=sha256_bytes(
                f"gitlink:{e['_oid']}".encode()), size=0)
            continue
        try:
            st = os.lstat(abs_path)
        except FileNotFoundError:
            # Deleted-in-worktree paths are EXCLUDED from the snapshot: the
            # source snapshot describes real bytes present (ch4), and index
            # staging differs between writer and reviewer repro — a
            # missing-path entry would make reviewer reproduction fail
            # spuriously. Deletions are carried in the changes manifest.
            absent.append(path)
            continue
        ftype, content, size = _file_type_and_content(abs_path)
        e.update(type=ftype, content_sha256=sha256_bytes(content), size=size,
                 mode=_mode_for(abs_path, st))
    for path in absent:
        entries.pop(path)
    return entries, snapshot_sha(entries)


def snapshot_sha(entries: dict[str, dict]) -> str:
    return canonical_snapshot_sha256(
        [{"path": e["path"], "type": e["type"], "mode": e["mode"],
          "size": e["size"], "content_sha256": e["content_sha256"]}
         for e in entries.values()])


# --------------------------------------------------------------------------
# masking (redaction) — hash recorded AFTER masking, source kept separate
# --------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\\s\\S]*?"
               r"-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)\b"
               r"\s*[:=]\s*[\"']?[^\s\"']{8,}"),
]
REDACTED = "[REDACTED]"


def redact_text(text: str) -> tuple[str, int]:
    hits = 0
    for pat in _SECRET_PATTERNS:
        text, n = pat.subn(REDACTED, text)
        hits += n
    return text, hits


# --------------------------------------------------------------------------
# preview truncation (display-only; never claimed complete)
# --------------------------------------------------------------------------

class Preview:
    __slots__ = ("text", "truncated", "shown_bytes", "total_bytes")

    def __init__(self, text, truncated, shown_bytes, total_bytes):
        self.text, self.truncated = text, truncated
        self.shown_bytes, self.total_bytes = shown_bytes, total_bytes


def preview_bytes(data: bytes, limit: int = PREVIEW_LIMIT) -> Preview:
    """Truncation is display-only. complete must never be claimed for a
    truncated view (WORKSPACE.md ch4); callers get truncated=True."""
    if len(data) <= limit:
        return Preview(data.decode("utf-8", "replace"), False, len(data),
                       len(data))
    return Preview(data[:limit].decode("utf-8", "replace"), True, limit,
                   len(data))


# --------------------------------------------------------------------------
# artifact store: private-first, then paged export
# --------------------------------------------------------------------------

class ListRegistrySink:
    """Default in-test sink: rows kept in memory, schema-checked."""

    def __init__(self):
        self.rows: list[dict] = []

    def insert_artifact(self, row: dict) -> None:
        self._check(row)
        for prev in self.rows:
            if (prev["attempt_id"], prev["relative_path"]) == \
                    (row["attempt_id"], row["relative_path"]):
                raise ArtifactError(
                    REASON_REGISTRY_CONFLICT,
                    f"{row['attempt_id']}|{row['relative_path']}")
        self.rows.append(dict(row))

    def get(self, artifact_id: str) -> dict | None:
        for r in self.rows:
            if r["artifact_id"] == artifact_id:
                return r
        return None

    @staticmethod
    def _check(row: dict) -> None:
        if row["section"] not in ARTIFACT_SECTIONS:
            raise ArtifactError(REASON_INVALID_ROW, f"section {row['section']}")
        if row["visibility"] not in ARTIFACT_VISIBILITIES:
            raise ArtifactError(REASON_INVALID_ROW, "visibility")
        if row["byte_length"] < 0 or len(row["sha256"]) != 64:
            raise ArtifactError(REASON_INVALID_ROW, "byte_length/sha256")
        for col in ("redacted", "complete", "applyable"):
            if row[col] not in (0, 1):
                raise ArtifactError(REASON_INVALID_ROW, col)
        if row["redacted"] == 1 and row["applyable"] == 1:
            raise ArtifactError(REASON_INVALID_ROW,
                                "redacted artifacts are never applyable")


class SqliteRegistrySink:
    """INSERT-only sink against the frozen artifacts table (schema.sql).
    Never DDL: the schema is P1-owned, read-only for P3."""

    COLUMNS = ("artifact_id", "job_id", "attempt_id", "result_revision",
               "section", "visibility", "relative_path", "media_type",
               "byte_length", "sha256", "source_sha256", "redacted",
               "complete", "applyable", "created_at")

    def __init__(self, conn):
        self.conn = conn

    def insert_artifact(self, row: dict) -> None:
        ListRegistrySink._check(row)
        cols = ", ".join(self.COLUMNS)
        ph = ", ".join("?" for _ in self.COLUMNS)
        try:
            self.conn.execute(
                f"INSERT INTO artifacts ({cols}) VALUES ({ph})",
                tuple(row[c] for c in self.COLUMNS))
            self.conn.commit()
        except Exception as exc:  # UNIQUE(attempt_id,relative_path) etc.
                raise ArtifactError(REASON_REGISTRY_CONFLICT, str(exc)) from exc

    def get(self, artifact_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        r = cur.fetchone()
        return dict(r) if r is not None else None


class ArtifactRecord:
    __slots__ = ("artifact_id", "row", "path")

    def __init__(self, artifact_id, row, path):
        self.artifact_id, self.row, self.path = artifact_id, row, path


class ArtifactStore:
    """Private directory + registry. Order rule (ch4): save_private first,
    exports derive from stored private bytes — never from live files."""

    def __init__(self, root, *, sink=None, clock=time.time,
                 cursor_ttl: float = CURSOR_TTL_SECONDS,
                 cursor_secret: bytes | None = None):
        self.root = os.path.realpath(str(root))
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.sink = sink if sink is not None else ListRegistrySink()
        self.clock = clock
        self.cursor_ttl = cursor_ttl
        self.cursor_secret = cursor_secret or secrets.token_bytes(32)

    # -- private ------------------------------------------------------------
    def save_private(self, *, job_id: str, attempt_id: str, result_revision: int,
                     section: str, relative_path: str, media_type: str,
                     data: bytes, applyable: int = 0,
                     source_sha256: str | None = None) -> ArtifactRecord:
        path = self._private_path(attempt_id, relative_path)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ArtifactError(REASON_REGISTRY_CONFLICT,
                                f"{attempt_id}|{relative_path}") from exc
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        row = self._row(job_id=job_id, attempt_id=attempt_id,
                        result_revision=result_revision, section=section,
                        visibility="private", relative_path=relative_path,
                        media_type=media_type, data=data,
                        redacted=0, complete=1, applyable=applyable,
                        source_sha256=source_sha256,
                        sha_override=sha256_bytes(data))
        self.sink.insert_artifact(row)
        return ArtifactRecord(row["artifact_id"], row, path)

    def _stored_path(self, row: dict) -> str:
        base = (self._private_path if row["visibility"] == "private"
                else self._export_path)
        return base(row["attempt_id"], row["relative_path"])

    def read_private(self, artifact_id: str) -> bytes:
        row = self.sink.get(artifact_id)
        if row is None or row["visibility"] != "private":
            raise ArtifactError(REASON_ORDER,
                                f"no private artifact {artifact_id}")
        with open(self._stored_path(row), "rb") as f:
            return f.read()

    # -- export -------------------------------------------------------------
    def export_masked(self, *, private: ArtifactRecord, job_id: str,
                      attempt_id: str, result_revision: int,
                      relative_path: str) -> "ExportSession":
        """Redact a stored PRIVATE artifact and register the masked export.

        Hashing is over post-masking bytes; source_sha256 pins the original
        (ch4: exported hash is distinct from source hash). redacted=1 forces
        applyable=0. Export requires the private artifact already saved —
        ORDER_VIOLATION otherwise.
        """
        priv_row = self.sink.get(private.artifact_id)
        if priv_row is None:  # private must be stored first (ch4)
            raise ArtifactError(REASON_ORDER, "export before private store")
        with open(self._stored_path(priv_row), "rb") as f:
            raw = f.read()
        try:
            masked_text, hits = redact_text(raw.decode("utf-8"))
            masked_bytes = masked_text.encode("utf-8")
        except UnicodeDecodeError:
            # binary: masked text is undefined; export raw bytes + hash (ch3)
            masked_bytes, hits = raw, 0
        # registry path is namespaced apart from the private row: the frozen
        # schema has UNIQUE(attempt_id, relative_path)
        export_rel = "export/" + relative_path
        row = self._row(job_id=job_id, attempt_id=attempt_id,
                        result_revision=result_revision, section="changes"
                        if relative_path.startswith("changes/") else "content",
                        visibility="export", relative_path=export_rel,
                        media_type="text/plain; charset=utf-8",
                        data=masked_bytes, redacted=1 if hits else 0,
                        complete=1,  # full masked document (paging is separate)
                        applyable=0, source_sha256=priv_row["sha256"],
                        sha_override=sha256_bytes(masked_bytes))
        self.sink.insert_artifact(row)
        rec = ArtifactRecord(row["artifact_id"], row,
                             self._export_path(attempt_id, export_rel))
        os.makedirs(os.path.dirname(rec.path), mode=0o700, exist_ok=True)
        try:
            fd = os.open(rec.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ArtifactError(REASON_REGISTRY_CONFLICT, export_rel) from exc
        try:
            os.write(fd, masked_bytes)
        finally:
            os.close(fd)
        return ExportSession(self, rec, masked_bytes, total_hits=hits)

    def save_export_page(self, *, private: ArtifactRecord, job_id: str,
                         attempt_id: str, result_revision: int,
                         relative_path: str, page_bytes: bytes,
                         complete: bool) -> ArtifactRecord:
        """Register one export page. A page smaller than the full document
        is NEVER complete (ch4) — passing complete=True is an error.
        A single page above MAX_SINGLE_EXPORT_BYTES is refused (T11: the
        1 MiB bound forces paging, not one giant page)."""
        if len(page_bytes) > MAX_SINGLE_EXPORT_BYTES:
            raise ArtifactError(
                REASON_TOO_LARGE,
                f"page {len(page_bytes)} > {MAX_SINGLE_EXPORT_BYTES}; "
                f"export must be paged (ch4 preview-only truncation)")
        priv_row = self.sink.get(private.artifact_id)
        if priv_row is None:
            raise ArtifactError(REASON_ORDER, "export before private store")
        with open(self._stored_path(priv_row), "rb") as f:
            full = sha256_bytes(f.read())
        is_full = sha256_bytes(page_bytes) == full
        if complete and not is_full:
            raise ArtifactError(REASON_TRUNCATED,
                                "truncated page marked complete")
        row = self._row(job_id=job_id, attempt_id=attempt_id,
                        result_revision=result_revision, section="changes",
                        visibility="export", relative_path=relative_path,
                        media_type="text/x-git-patch", data=page_bytes,
                        redacted=0, complete=1 if is_full else 0,
                        applyable=1 if is_full else 0, source_sha256=full,
                        sha_override=sha256_bytes(page_bytes))
        self.sink.insert_artifact(row)
        return ArtifactRecord(row["artifact_id"], row, None)

    # -- internals ------------------------------------------------------------
    def _private_path(self, attempt_id: str, relative_path: str) -> str:
        return self._safe_join("private", attempt_id, relative_path)

    def _export_path(self, attempt_id: str, relative_path: str) -> str:
        return self._safe_join("export", attempt_id, relative_path)

    def _safe_join(self, *parts: str) -> str:
        joined = os.path.realpath(os.path.join(self.root, *parts))
        if not joined.startswith(self.root + os.sep):
            raise ArtifactError(REASON_INVALID_ROW,
                                f"artifact path escapes store root: {parts}")
        return joined

    def _row(self, *, job_id, attempt_id, result_revision, section,
             visibility, relative_path, media_type, data, redacted, complete,
             applyable, source_sha256, sha_override) -> dict:
        sha = sha_override or sha256_bytes(data)
        row = {
            "artifact_id": new_artifact_id(),  # art_<UUIDv4> (MCP.md §2);
            # content binding lives in sha256/source_sha256, never in the id
            "job_id": job_id, "attempt_id": attempt_id,
            "result_revision": result_revision, "section": section,
            "visibility": visibility, "relative_path": relative_path,
            "media_type": media_type, "byte_length": len(data),
            "sha256": sha, "source_sha256": source_sha256,
            "redacted": redacted, "complete": complete,
            "applyable": applyable,
            "created_at": _utcnow(),
            "_file_path": (self._private_path(attempt_id, relative_path)
                           if visibility == "private"
                           else self._export_path(attempt_id, relative_path)),
        }
        return row


# --------------------------------------------------------------------------
# paged export with signed, expiring cursors
# --------------------------------------------------------------------------

class Page:
    __slots__ = ("data", "index", "total_pages", "sha256", "is_last",
                 "next_cursor")

    def __init__(self, data, index, total_pages, sha256, is_last, next_cursor):
        self.data, self.index = data, index
        self.total_pages, self.sha256 = total_pages, sha256
        self.is_last, self.next_cursor = is_last, next_cursor


class ExportSession:
    """Walk one export artifact in pages ≤ chunk limit.

    Cursor = artifact generation + page index + expiry + HMAC; any of
    index/expiry tamper → CURSOR_MUTATED; past expiry → CURSOR_EXPIRED;
    artifact from another generation (e.g. after re-export, T12 stale)
    → CURSOR_STALE.
    """

    def __init__(self, store: ArtifactStore, record: ArtifactRecord,
                 masked_bytes: bytes, *, total_hits: int = 0):
        self._store = store
        self.record = record
        self.total_hits = total_hits
        self._data = masked_bytes
        self._n = max(1, -(-len(masked_bytes) // EXPORT_CHUNK_LIMIT))

    def first_cursor(self) -> str:
        return self._cursor(0)

    def _cursor(self, index: int) -> str:
        expires = int(self._store.clock() + self._store.cursor_ttl)
        body = f"{self.record.artifact_id}|{index}|{expires}"
        sig = hmac.new(self._store.cursor_secret, body.encode(),
                       hashlib.sha256).hexdigest()[:32]
        return f"v1.{body}.{sig}"

    def _parse(self, cursor: str) -> tuple[str, int, int]:
        parts = cursor.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise ArtifactError(REASON_CURSOR_MUTATED, "malformed cursor")
        body, sig = parts[1], parts[2]
        if body.count("|") != 2:
            raise ArtifactError(REASON_CURSOR_MUTATED, "malformed body")
        want = hmac.new(self._store.cursor_secret, body.encode(),
                        hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(want, sig):
            raise ArtifactError(REASON_CURSOR_MUTATED, "signature mismatch")
        aid, index, expires = body.split("|")
        try:
            return aid, int(index), int(expires)
        except ValueError as exc:
            raise ArtifactError(REASON_CURSOR_MUTATED, str(exc)) from exc

    def page(self, cursor: str) -> Page:
        aid, index, expires = self._parse(cursor)
        if aid != self.record.artifact_id:
            raise ArtifactError(REASON_CURSOR_STALE, aid)
        row = self._store.sink.get(aid)
        if row is None or row.get("sha256") != self.record.row["sha256"]:
            raise ArtifactError(REASON_CURSOR_STALE, aid)
        if self._store.clock() > expires:
            raise ArtifactError(REASON_CURSOR_EXPIRED, aid)
        if not 0 <= index < self._n:
            raise ArtifactError(REASON_CURSOR_MUTATED, f"index {index}")
        start = index * EXPORT_CHUNK_LIMIT
        chunk = self._data[start:start + EXPORT_CHUNK_LIMIT]
        last = index + 1 >= self._n
        return Page(chunk, index, self._n, sha256_bytes(chunk), last,
                    None if last else self._cursor(index + 1))

    def drain(self) -> tuple[bytes, list[Page]]:
        out, pages, cur = b"", [], self.first_cursor()
        while True:
            p = self.page(cur)
            out += p.data
            pages.append(p)
            if p.is_last:
                return out, pages
            cur = p.next_cursor


# --------------------------------------------------------------------------
# result collection (WORKSPACE.md ch4) and reviewer checks (ch5)
# --------------------------------------------------------------------------

def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def collect_changes(worktree, base_sha: str, *,
                    baseline: dict[str, dict], baseline_sha: str,
                    expected_ignored: tuple[str, ...] = ()) -> dict:
    """Collect writer results vs baseline. Never a single `git diff` (ch4):

    - tracked patch via `git diff --no-ext-diff --no-textconv --binary -M`
    - untracked via `git ls-files --others --exclude-standard -z`
      cross-checked against the baseline manifest
    - ignored-but-requested outputs explicitly collected + reported
    - changes manifest with before/after sha256, sizes, modes; renames keep
      both paths; binary/link/delete are never replaced by empty text.

    A baseline from a DIFFERENT revision is a STALE check and is refused
    (ch5, T12): every baseline-tracked path must either still be listed or
    show as deleted in the base diff — a tracked path that simply vanished
    from a foreign baseline is unexplainable and raises SNAPSHOT_CHECK_STALE.
    """
    cur_entries, cur_snapshot = tree_manifest(worktree)
    tracked_now = set(git(worktree, "ls-files", "-z").stdout.decode(
        "utf-8", "surrogateescape").split("\0"))
    others_now = [p for p in git(
        worktree, "ls-files", "--others", "--exclude-standard",
        "-z").stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    deleted_in_diff = set(git(worktree, "diff", "--name-only", "--no-renames",
                              "--diff-filter=D", base_sha, "--").stdout
                          .decode("utf-8", "surrogateescape").split("\n"))
    stale = [p for p, e in baseline.items()
             if e.get("tracked") and p not in tracked_now
             and p not in deleted_in_diff]
    if stale:
        raise ArtifactError(
            REASON_SNAPSHOT_STALE,
            f"baseline not from this revision: {stale[:5]} (baseline_sha="
            f"{baseline_sha[:16]}…) — stale checks must not be reused")

    patch = git(worktree, "diff", "--no-ext-diff", "--no-textconv",
                "--binary", "-M", base_sha).stdout
    name_status = git(worktree, "diff", "--no-ext-diff", "--no-textconv",
                      "-M", "--name-status", "-z", base_sha).stdout
    ignored = git(worktree, "status", "--porcelain=v1", "-z",
                  "--ignored", "--untracked-files=all").stdout.split(b"\0")

    changes: list[dict] = []
    capability_flags: set[str] = set()

    # -z name-status records: "XY\0path\0", renames "R100\0old\0new\0"
    parts = name_status.decode("utf-8", "surrogateescape").split("\0")
    i = 0
    while i < len(parts) - 1:
        st_code = parts[i]
        if not st_code:
            i += 1
            continue
        kind = st_code[0]
        if kind == "R" or kind == "C":
            old, new = parts[i + 1], parts[i + 2]
            i += 3
            path_pair = (old, new)
        else:
            new = parts[i + 1]
            old = new
            i += 2
            path_pair = None
        kind_map = {"A": "added", "M": "modified", "D": "deleted",
                    "R": "renamed", "C": "copied", "T": "typechange"}
        change_kind = kind_map.get(kind, kind)
        base_e = baseline.get(old) or baseline.get(new)
        cur_e = cur_entries.get(new)
        entry = _change_entry(worktree, new, change_kind, base_e, cur_e,
                              old_path=path_pair[0] if path_pair else None)
        if entry["file_type"] == "gitlink":
            capability_flags.add("CAPABILITY_UNVERIFIED")
        changes.append(entry)

    baseline_paths = set(baseline)
    others = [p for p in others_now if p]
    for path in others:
        if path in cur_entries and cur_entries[path].get("tracked"):
            continue  # already covered by tracked diff
        base_e = None if path not in baseline_paths else baseline[path]
        origin = "input" if path in baseline_paths else "writer"
        cur_e = cur_entries[path]
        entry = _change_entry(worktree, path, "added", base_e, cur_e,
                              old_path=None)
        entry["origin"] = origin
        if entry["file_type"] == "gitlink":
            capability_flags.add("CAPABILITY_UNVERIFIED")
        changes.append(entry)

    # untracked baseline files the writer deleted: git cannot show that, so
    # record it explicitly (ch4: don't end with one git diff)
    for path, e in baseline.items():
        if (not e.get("tracked") and path not in tracked_now
                and path not in others_now
                and not os.path.lexists(os.path.join(str(worktree), path))):
            entry = _change_entry(worktree, path, "deleted", e, None,
                                  old_path=None)
            entry["origin"] = "input"
            changes.append(entry)

    # ignored-but-requested: explicit collection, never silently dropped;
    # other ignored files are reported in the exclusion policy (ch3).
    excluded: list[str] = []
    for raw in ignored:
        if not raw or not raw.startswith(b"!!"):
            continue
        body = raw[3:].decode("utf-8", "surrogateescape")
        if body in expected_ignored:
            # explicit collection of ignored requested output (ch4)
            entry = _change_entry(worktree, body, "added", None, None,
                                  old_path=None)
            entry["origin"] = "ignored_requested"
            changes.append(entry)
        else:
            excluded.append(body)

    # completeness invariant (ch4 "never end with one git diff"): every path
    # in the final tree manifest must be represented — index-only entries
    # (e.g. gitlinks) can be invisible to the worktree diff.
    reported = {e["relative_path"] for e in changes}
    reported |= {e["old_path"] for e in changes if e.get("old_path")}
    for path, e in cur_entries.items():
        if path in reported:
            continue
        base_e = baseline.get(path)
        entry = _change_entry(worktree, path,
                              "modified" if base_e else "added",
                              base_e, e, old_path=None)
        entry["origin"] = "tracked" if e.get("tracked") else "writer"
        if entry["file_type"] == "gitlink":
            capability_flags.add("CAPABILITY_UNVERIFIED")
        changes.append(entry)

    changes.sort(key=lambda e: (e["relative_path"], e.get("old_path") or ""))
    doc = {
        "schema": "changes-manifest/1",
        "base_sha": base_sha,
        "baseline_manifest_sha256": baseline_sha,
        "source_snapshot_sha256": cur_snapshot,
        "tracked_patch_sha256": sha256_bytes(patch),  # distinct hash (ch4)
        "entries": changes,
        "capability_flags": sorted(capability_flags),
        "excluded_policy": {
            "cache": "none",
            "ignored_not_requested": sorted(excluded),
            "note": "normal sources are never silently excluded; ignored "
                    "files listed here were not requested as outputs",
        },
    }
    return {"manifest_doc": doc, "tracked_patch": patch,
            "source_snapshot_sha256": cur_snapshot,
            "entries": changes, "tree_manifest": cur_entries,
            "capability_flags": sorted(capability_flags),
            "excluded_ignored": sorted(excluded),
            # ch5: reviewer reproduces patch + ALL new-file entries, inputs
            # included — a missing new file would be BLOCKED at review.
            "new_files": [e for e in changes
                          if e["change_kind"] == "added" and e["origin"] in
                          ("writer", "input", "ignored_requested")]}


def _change_entry(worktree, path, change_kind, base_e, cur_e, *, old_path):
    abs_path = os.path.join(str(worktree), path)
    file_type, size, after_sha, mode = None, None, None, None
    for src in (cur_e, base_e):  # gitlink: reported, never touched (ch3)
        if src is not None and src.get("mode") == "160000":
            return {
                "relative_path": path, "old_path": old_path,
                "change_kind": change_kind, "file_type": "gitlink",
                "before_sha256": (base_e or {}).get("content_sha256"),
                "after_sha256": (cur_e or {}).get("content_sha256"),
                "size": 0, "mode": "160000", "artifact_id": None,
                "origin": "tracked",
            }
    if cur_e is not None and os.path.lexists(abs_path):
        file_type = cur_e["type"]
        size = cur_e["size"]
        after_sha = cur_e["content_sha256"]
        mode = cur_e["mode"]
    elif change_kind == "deleted":
        file_type, size, mode = "deleted", 0, None
    st = None
    if cur_e is None and os.path.lexists(abs_path):
        st = os.lstat(abs_path)
        file_type = "link" if statmod.S_ISLNK(st.st_mode) else "file"
        mode = _mode_for(abs_path, st)
        if file_type == "link":
            tgt = os.fsencode(os.readlink(abs_path))
            after_sha, size = sha256_bytes(tgt), len(tgt)
        else:
            with open(abs_path, "rb") as f:
                data = f.read()
            if _is_binary(data):
                file_type = "binary"
            after_sha, size = sha256_bytes(data), len(data)
    entry = {
        "relative_path": path,
        "old_path": old_path,
        "change_kind": change_kind,
        "file_type": file_type or "unknown",
        "before_sha256": base_e.get("content_sha256") if base_e else None,
        "after_sha256": after_sha,   # deleted → None (never empty text, ch4)
        "size": size if size is not None else 0,
        "mode": mode,
        "artifact_id": None,  # filled by the collector when stored
        "origin": "tracked",
    }
    if file_type == "binary":
        entry["preview"] = None  # binary is bytes-archive + hash only (ch3)
    return entry


def snapshot_diff(expected_snapshot_sha: str, expected_entries: dict,
                  actual_snapshot_sha: str, actual_entries: dict) -> dict:
    """Reviewer-side check (ch5): exact repro or an explicit BLOCKED-style
    result. Reusing a stale check surfaces here as missing/extra/changed."""
    exp = {p: e for p, e in expected_entries.items()}
    act = {p: e for p, e in actual_entries.items()}
    missing = sorted(p for p in exp if p not in act)          # incl. new files
    extra = sorted(p for p in act if p not in exp)
    changed = sorted(p for p in exp if p in act and
                     exp[p]["content_sha256"] != act[p]["content_sha256"])
    return {
        "match": not (missing or extra or changed) and (
            expected_snapshot_sha == actual_snapshot_sha),
        "snapshot_expected": expected_snapshot_sha,
        "snapshot_actual": actual_snapshot_sha,
        "missing": missing, "extra": extra, "changed": changed,
    }


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + \
        ".%06dZ" % (int(time.time() * 1_000_000) % 1_000_000)
