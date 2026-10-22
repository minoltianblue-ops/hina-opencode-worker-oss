"""SQLite storage: migration, CAS updates, append-only state journal.

Normative inputs: docs/contracts/schema.sql (byte-identical copy shipped as
schema.sql in this package) and docs/contracts/STORAGE.md.

Invariants enforced here:
- PRAGMAs: foreign_keys=ON, journal_mode=WAL, synchronous=FULL,
  busy_timeout=5000ms default. user_version migration; a newer DB is
  never read by an older store (no downgrade).
- Single supervisor writer guarded by an OS singleton lock; short
  BEGIN IMMEDIATE transactions. No external I/O inside a transaction.
- CAS: jobs.version is checked and incremented in the UPDATE; the
  state_events journal row (record_version = new version) is appended in
  the SAME transaction, so UNIQUE(job_id, record_version) makes a lost
  or duplicated journal impossible.
- Workspace lease: one held lease per canonical path; fencing tokens
  increase monotonically; stale tokens are rejected. TTL expiry never
  releases a write lease.
- Storage pressure: SQLITE_FULL surfaces as StorageFullError (STORAGE_FULL)
  and SQLITE_BUSY/LOCKED as DatabaseLockedError; committed data stays
  intact and the transaction is fully rolled back.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .state import (
    NEEDS_ATTENTION,
    InvariantFailureError,
    StateMachine,
)

SCHEMA_PATH = __import__("pathlib").Path(__file__).resolve().parent / "schema.sql"
MIGRATION_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5000

# States that occupy the one-active-attempt-per-job slot (schema partial index).
ACTIVE_ATTEMPT_STATES = (
    "starting",
    "running",
    "collecting",
    "waiting_approval",
    "cancel_requested",
)

# requests.phase values that a restart must reconcile (STORAGE.md §3 ④).
UNRESOLVED_REQUEST_PHASES = ("prepared", "sending", "acknowledged")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


class StoreError(RuntimeError):
    code = "STORE_ERROR"


class CasConflictError(StoreError):
    code = "CAS_CONFLICT"

    def __init__(self, job_id: str, expected_version: int, actual: int | None):
        super().__init__(
            f"CAS conflict job={job_id} expected_version={expected_version} actual={actual}"
        )
        self.job_id = job_id
        self.expected_version = expected_version
        self.actual_version = actual


class StorageFullError(StoreError):
    code = "STORAGE_FULL"


class DatabaseLockedError(StoreError):
    code = "DB_LOCKED"


class DatabaseTooNewError(StoreError):
    code = "DB_TOO_NEW"


class NotFoundError(StoreError):
    code = "NOT_FOUND"


class LeaseHeldError(StoreError):
    code = "BUSY"


class StaleFencingError(StoreError):
    code = "STALE_FENCING"


class AlreadyRunningError(StoreError):
    code = "ALREADY_RUNNING"


_SQLITE_FULL = 13
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6


def _map_operational_error(e: sqlite3.OperationalError) -> StoreError:
    code = getattr(e, "sqlite_errorcode", None)
    msg = str(e).lower()
    if code == _SQLITE_FULL or "database or disk is full" in msg:
        return StorageFullError(f"storage full: {e}")
    if code in (_SQLITE_BUSY, _SQLITE_LOCKED) or "database is locked" in msg:
        return DatabaseLockedError(f"database locked: {e}")
    return e


class _Umask0077:
    """STORAGE.md §1: apply umask 0077 before creating runtime files."""

    def __enter__(self):
        self._old = os.umask(0o077)
        return self

    def __exit__(self, *exc):
        os.umask(self._old)
        return False


class SupervisorLock:
    """OS singleton lock (run/supervisor.lock). Second acquire fails.

    flock is per open-file-description, so this blocks a second
    supervisor even in the same process. Ownership is the lock itself;
    PID alone is never used to decide ownership.
    """

    def __init__(self, lock_path: str | os.PathLike[str]) -> None:
        import fcntl  # POSIX only; Mac mini is the target runtime.

        self._fcntl = fcntl
        self.path = os.fspath(lock_path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise AlreadyRunningError("lock already held by this handle")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with _Umask0077():
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._fcntl.flock(fd, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise AlreadyRunningError(
                f"another supervisor holds {self.path}"
            ) from e
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "SupervisorLock":
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class Store:
    """One connection = one writer. Keep transactions short; never do
    network/CLI I/O inside tx()."""

    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self.conn = conn
        self.path = path
        self.sm = StateMachine()

    # ---------------------------------------------------------------- open

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> "Store":
        path = os.fspath(path)
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        with _Umask0077():
            conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0)
        conn.isolation_level = None  # manual BEGIN IMMEDIATE transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.close()
            raise StoreError(f"could not enable WAL (got {mode!r})")
        conn.execute("PRAGMA synchronous = FULL")
        store = cls(conn, path)
        store._migrate()
        return store

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > MIGRATION_VERSION:
            raise DatabaseTooNewError(
                f"db user_version={version} newer than store migration {MIGRATION_VERSION}; "
                "refusing to read a newer DB"
            )
        if version == MIGRATION_VERSION:
            return
        # Backup-before-migration is a supervisor (P4) duty; P1 only ever
        # installs a fresh v1 schema from the contract DDL.
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        got = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if got != MIGRATION_VERSION:
            raise StoreError(f"migration left user_version={got}")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ tx/exec

    @contextmanager
    def tx(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise _map_operational_error(e) from e
        try:
            yield self.conn
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        try:
            self.conn.execute("COMMIT")
        except sqlite3.OperationalError as e:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise _map_operational_error(e) from e

    def exec(self, conn: sqlite3.Connection, sql: str, params: tuple = ()):
        """Write helper with storage-error mapping (use inside tx())."""
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            raise _map_operational_error(e) from e

    # -------------------------------------------------------------- reads

    def get_job(self, job_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return row

    def get_request(self, principal_id: str, request_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM requests WHERE principal_id = ? AND request_id = ?",
            (principal_id, request_id),
        ).fetchone()

    def active_attempt(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM attempts WHERE job_id = ? "
            f"AND state IN {ACTIVE_ATTEMPT_STATES!r}",
            (job_id,),
        ).fetchone()

    def journal(self, job_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM state_events WHERE job_id = ? ORDER BY record_version",
                (job_id,),
            )
        )

    def integrity_check(self) -> str:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0]

    def diagnostics(self) -> dict:
        return {
            "journal_mode": self.conn.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": self.conn.execute("PRAGMA synchronous").fetchone()[0],
            "foreign_keys": self.conn.execute("PRAGMA foreign_keys").fetchone()[0],
            "busy_timeout": self.conn.execute("PRAGMA busy_timeout").fetchone()[0],
            "user_version": self.conn.execute("PRAGMA user_version").fetchone()[0],
            "page_count": self.conn.execute("PRAGMA page_count").fetchone()[0],
        }

    def set_max_page_count(self, pages: int) -> int:
        """Admin/test hook: cap the DB size so real SQLITE_FULL is
        triggered by sqlite itself instead of a mocked error."""
        return self.conn.execute(
            f"PRAGMA max_page_count = {int(pages)}"
        ).fetchone()[0]

    # ------------------------------------------------------- job creation

    def insert_job(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        principal_id: str,
        goal: str,
        requested_model: str,
        profile: str,
        repository_path: str,
        base_revision: str | None,
        now: str,
    ) -> None:
        self.exec(
            conn,
            "INSERT INTO jobs (job_id, principal_id, goal, requested_model, profile,"
            " repository_path, base_revision, state, active_attempt_id, version,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,NULL,0,?,?)",
            (
                job_id,
                principal_id,
                goal,
                requested_model,
                profile,
                repository_path,
                base_revision,
                self.sm.initial,
                now,
                now,
            ),
        )
        # Creation is the journal exception (STORAGE.md §2) but we still
        # append record_version=0 for a complete audit trail.
        self._append_event(
            conn,
            job_id=job_id,
            attempt_id=None,
            from_state=None,
            to_state=self.sm.initial,
            reason_code="intake",
            observed_at=now,
            record_version=0,
        )

    def insert_request(
        self,
        conn: sqlite3.Connection,
        *,
        principal_id: str,
        request_id: str,
        tool: str,
        payload_sha256: str,
        normalized_json: str,
        job_id: str | None,
        attempt_id: str | None,
        phase: str,
        receipt: dict | None,
        created_at: str,
        updated_at: str,
    ) -> None:
        self.exec(
            conn,
            "INSERT INTO requests (principal_id, request_id, tool, payload_sha256,"
            " normalized_json, job_id, attempt_id, phase, receipt_json, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                principal_id,
                request_id,
                tool,
                payload_sha256,
                normalized_json,
                job_id,
                attempt_id,
                phase,
                json.dumps(receipt, sort_keys=True, separators=(",", ":"))
                if receipt is not None
                else None,
                created_at,
                updated_at,
            ),
        )

    def insert_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        job_id: str,
        ordinal: int,
        executor_id: str,
        model: str,
        billing_class: str,
        state: str,
        contract_revision: str,
        binary_sha256: str,
        workspace_path: str | None = None,
    ) -> None:
        self.exec(
            conn,
            "INSERT INTO attempts (attempt_id, job_id, ordinal, executor_id, model,"
            " billing_class, state, contract_revision, binary_sha256, workspace_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                job_id,
                ordinal,
                executor_id,
                model,
                billing_class,
                state,
                contract_revision,
                binary_sha256,
                workspace_path,
            ),
        )

    def set_active_attempt(
        self, conn: sqlite3.Connection, job_id: str, attempt_id: str, expected_version: int, now: str
    ) -> int:
        """STORAGE.md §7: active_attempt_id set inside the intake tx; CAS.
        Same-job ownership is checked in-tx before the pointer update: the
        FK only proves the attempt exists, not that it belongs to this job."""
        owner = conn.execute(
            "SELECT job_id FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if owner is None or owner["job_id"] != job_id:
            raise InvariantFailureError(
                f"attempt {attempt_id} does not belong to job {job_id}"
            )
        cur = self.exec(
            conn,
            "UPDATE jobs SET active_attempt_id = ?, version = version + 1,"
            " updated_at = ? WHERE job_id = ? AND version = ?",
            (attempt_id, now, job_id, expected_version),
        )
        if cur.rowcount != 1:
            actual = conn.execute(
                "SELECT version FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            raise CasConflictError(job_id, expected_version, actual[0] if actual else None)
        return expected_version + 1

    # ------------------------------------------------- CAS + journal core

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        attempt_id: str | None,
        from_state: str | None,
        to_state: str,
        reason_code: str,
        observed_at: str,
        record_version: int,
    ) -> None:
        self.exec(
            conn,
            "INSERT INTO state_events (job_id, attempt_id, from_state, to_state,"
            " reason_code, observed_at, record_version) VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                attempt_id,
                from_state,
                to_state,
                reason_code,
                observed_at,
                record_version,
            ),
        )

    def _transition_job_conn(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        expected_version: int,
        to_state: str,
        reason_code: str,
        *,
        attempt_id: str | None = None,
        attention_reason: str | None = None,
        reconcile_proof: str | None = None,
        explicit_followup: bool = False,
        now: str | None = None,
    ) -> int:
        """CAS state change + journal append in the caller's transaction."""
        now = now or utc_now()
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        if row["version"] != expected_version:
            raise CasConflictError(job_id, expected_version, row["version"])
        frm = row["state"]
        self.sm.require_transition(frm, to_state)
        self.sm.require_reconcile_proof(frm, to_state, reconcile_proof)
        self.sm.require_followup(frm, to_state, explicit_followup)
        if to_state == NEEDS_ATTENTION and not (attention_reason and attention_reason.strip()):
            raise InvariantFailureError(
                "needs_attention requires an attention_reason (execution unclear /"
                " evidence incomplete / backend response lost)"
            )
        cur = self.exec(
            conn,
            "UPDATE jobs SET state = ?, version = version + 1, updated_at = ?,"
            " attention_reason = ? WHERE job_id = ? AND version = ?",
            (
                to_state,
                now,
                attention_reason if to_state == NEEDS_ATTENTION else None,
                job_id,
                expected_version,
            ),
        )
        if cur.rowcount != 1:
            actual = conn.execute(
                "SELECT version FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            raise CasConflictError(job_id, expected_version, actual[0] if actual else None)
        new_version = expected_version + 1
        self._append_event(
            conn,
            job_id=job_id,
            attempt_id=attempt_id,
            from_state=frm,
            to_state=to_state,
            reason_code=reason_code,
            observed_at=now,
            record_version=new_version,
        )
        # Attempt state is NOT blindly mirrored: waiting_model_selection is a
        # job-only wait state (STORAGE.md §7 closes the prior attempt as
        # failed separately). Only needs_attention reconciles the attempt in
        # lockstep, and only when the attempt's own transition is legal.
        if attempt_id is not None and to_state == NEEDS_ATTENTION:
            self._update_attempt_state_conn(conn, attempt_id, NEEDS_ATTENTION)
        return new_version

    def transition_job(
        self,
        job_id: str,
        expected_version: int,
        to_state: str,
        reason_code: str,
        **kw,
    ) -> int:
        with self.tx() as conn:
            return self._transition_job_conn(
                conn, job_id, expected_version, to_state, reason_code, **kw
            )

    def _update_attempt_state_conn(
        self, conn: sqlite3.Connection, attempt_id: str, to_state: str
    ) -> None:
        row = conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"attempt {attempt_id} not found")
        frm = row["state"]
        if frm == to_state:
            return
        self.sm.require_transition(frm, to_state)
        ended = utc_now() if self.sm.is_terminal_for_attempt(to_state) else None
        self.exec(
            conn,
            "UPDATE attempts SET state = ?, ended_at = COALESCE(?, ended_at)"
            " WHERE attempt_id = ?",
            (to_state, ended, attempt_id),
        )

    def update_attempt_state(self, attempt_id: str, to_state: str) -> None:
        with self.tx() as conn:
            self._update_attempt_state_conn(conn, attempt_id, to_state)

    # ------------------------------------------------- crash-window policy

    def reconcile_after_restart(self, now: str | None = None) -> list[tuple[str, str]]:
        """STORAGE.md §3 ④. Returns [(request_id, plan)]:
        - prepared (never dispatched) -> 'resume' (caller may dispatch once)
        - sending/acknowledged (response lost) -> phase 'ambiguous',
          job+attempt -> needs_attention; re-dispatch is FORBIDDEN.
        needs_attention is never auto-moved to an active state here.
        """
        now = now or utc_now()
        plans: list[tuple[str, str]] = []
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT r.*, j.state AS job_state, j.version AS job_version"
                " FROM requests r JOIN jobs j ON j.job_id = r.job_id"
                f" WHERE r.phase IN {UNRESOLVED_REQUEST_PHASES!r}"
                " ORDER BY r.created_at, r.request_id",
            ).fetchall()
            for r in rows:
                if r["phase"] == "prepared":
                    plans.append((r["request_id"], "resume"))
                    continue
                self.exec(
                    conn,
                    "UPDATE requests SET phase = 'ambiguous', updated_at = ?"
                    " WHERE principal_id = ? AND request_id = ?",
                    (now, r["principal_id"], r["request_id"]),
                )
                self._transition_job_conn(
                    conn,
                    r["job_id"],
                    r["job_version"],
                    NEEDS_ATTENTION,
                    "crash_window_unproven",
                    attempt_id=r["attempt_id"],
                    attention_reason=(
                        f"request {r['request_id']} was in phase {r['phase']!r} at restart;"
                        " backend session/turn could not be reconciled"
                    ),
                    now=now,
                )
                plans.append((r["request_id"], "needs_attention"))
        return plans

    # ------------------------------------------------------ workspace lease

    def acquire_workspace_lease(
        self,
        *,
        canonical_path: str,
        job_id: str,
        attempt_id: str,
        executor_id: str,
        git_common_dir: str,
        base_revision: str,
        owner_pid: int | None = None,
        owner_started_at: str | None = None,
        now: str | None = None,
    ) -> int:
        """One held lease per canonical path. Returns the fencing token.
        TTL expiry never releases this lease."""
        now = now or utc_now()
        with self.tx() as conn:
            # canonical_path is the PK: one row per workspace, re-armed on
            # release with a strictly higher fencing token.
            existing = conn.execute(
                "SELECT lease_state, fencing_token FROM workspaces WHERE canonical_path = ?",
                (canonical_path,),
            ).fetchone()
            if existing is not None and existing["lease_state"] != "released":
                raise LeaseHeldError(
                    f"workspace {canonical_path} lease held (token={existing['fencing_token']})"
                )
            token = (existing["fencing_token"] if existing else 0) + 1
            if existing is None:
                self.exec(
                    conn,
                    "INSERT INTO workspaces (canonical_path, job_id, attempt_id, executor_id,"
                    " git_common_dir, base_revision, lease_state, fencing_token, owner_pid,"
                    " owner_started_at, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,'held',?,?,?,?,?)",
                    (
                        canonical_path,
                        job_id,
                        attempt_id,
                        executor_id,
                        git_common_dir,
                        base_revision,
                        token,
                        owner_pid,
                        owner_started_at,
                        now,
                        now,
                    ),
                )
            else:
                self.exec(
                    conn,
                    "UPDATE workspaces SET job_id = ?, attempt_id = ?, executor_id = ?,"
                    " git_common_dir = ?, base_revision = ?, lease_state = 'held',"
                    " fencing_token = ?, owner_pid = ?, owner_started_at = ?,"
                    " updated_at = ? WHERE canonical_path = ?",
                    (
                        job_id,
                        attempt_id,
                        executor_id,
                        git_common_dir,
                        base_revision,
                        token,
                        owner_pid,
                        owner_started_at,
                        now,
                        canonical_path,
                    ),
                )
            return token

    def release_workspace_lease(self, canonical_path: str, fencing_token: int, now: str | None = None) -> None:
        now = now or utc_now()
        with self.tx() as conn:
            cur = conn.execute(
                "SELECT fencing_token FROM workspaces"
                " WHERE canonical_path = ? AND lease_state = 'held'",
                (canonical_path,),
            ).fetchone()
            if cur is None:
                raise NotFoundError(f"no held lease for {canonical_path}")
            if cur["fencing_token"] != fencing_token:
                raise StaleFencingError(
                    f"stale fencing token {fencing_token} (current {cur['fencing_token']})"
                )
            self.exec(
                conn,
                "UPDATE workspaces SET lease_state = 'released', updated_at = ?"
                " WHERE canonical_path = ? AND fencing_token = ?",
                (now, canonical_path, fencing_token),
            )

    def assert_fencing(self, canonical_path: str, fencing_token: int) -> None:
        """A stale supervisor's write must not overwrite newer state."""
        cur = self.conn.execute(
            "SELECT fencing_token FROM workspaces"
            " WHERE canonical_path = ? AND lease_state = 'held'",
            (canonical_path,),
        ).fetchone()
        if cur is None or cur["fencing_token"] != fencing_token:
            raise StaleFencingError(
                f"fencing token {fencing_token} is not the current held lease"
                + (f" (current {cur['fencing_token']})" if cur else " (no held lease)")
            )
