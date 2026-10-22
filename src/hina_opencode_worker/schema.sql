-- Hina Worker v0.3 design DDL. Not an installed runtime database.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
PRAGMA user_version = 1;
CREATE TABLE jobs (
 job_id TEXT PRIMARY KEY,
 principal_id TEXT NOT NULL,
 goal TEXT NOT NULL,
 requested_model TEXT NOT NULL,
 profile TEXT NOT NULL CHECK(profile IN ('inspect','develop')),
 repository_path TEXT NOT NULL,
 base_revision TEXT,
 state TEXT NOT NULL CHECK(state IN ('accepted','starting','running','collecting','waiting_approval','waiting_model_selection','cancel_requested','completed','failed','cancelled','needs_attention')),
 verification_status TEXT NOT NULL DEFAULT 'not_run' CHECK(verification_status IN ('not_run','passed','failed','partial','unknown')),
 acceptance TEXT NOT NULL DEFAULT 'pending' CHECK(acceptance IN ('pending','accepted','rejected')),
 active_attempt_id TEXT,
 result_revision INTEGER,
 attention_reason TEXT,
 version INTEGER NOT NULL DEFAULT 0 CHECK(version>=0),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(active_attempt_id) REFERENCES attempts(attempt_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE attempts (
 attempt_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 ordinal INTEGER NOT NULL CHECK(ordinal>=1),
 executor_id TEXT NOT NULL,
 model TEXT NOT NULL,
 billing_class TEXT NOT NULL CHECK(billing_class IN ('free','go','unknown')),
 state TEXT NOT NULL CHECK(state IN ('accepted','starting','running','collecting','waiting_approval','waiting_model_selection','cancel_requested','completed','failed','cancelled','needs_attention')),
 contract_revision TEXT NOT NULL,
 binary_sha256 TEXT NOT NULL,
 backend_identity TEXT,
 session_id TEXT,
 turn_id TEXT,
 workspace_path TEXT,
 input_snapshot_sha256 TEXT,
 source_snapshot_sha256 TEXT,
 exit_code INTEGER,
 termination_signal TEXT,
 started_at TEXT,
 ended_at TEXT,
 usage_json TEXT CHECK(usage_json IS NULL OR json_valid(usage_json)),
 cost_state TEXT NOT NULL DEFAULT 'unknown' CHECK(cost_state IN ('unknown','provider_reported')),
 UNIQUE(job_id,ordinal)
);
CREATE UNIQUE INDEX one_active_attempt_per_job ON attempts(job_id)
 WHERE state IN ('starting','running','collecting','waiting_approval','cancel_requested');
CREATE TABLE requests (
 principal_id TEXT NOT NULL,
 request_id TEXT NOT NULL,
 tool TEXT NOT NULL CHECK(tool IN ('worker_start','worker_followup','worker_cancel','worker_approve')),
 payload_sha256 TEXT NOT NULL,
 normalized_json TEXT NOT NULL CHECK(json_valid(normalized_json)),
 job_id TEXT REFERENCES jobs(job_id),
 attempt_id TEXT REFERENCES attempts(attempt_id),
 phase TEXT NOT NULL CHECK(phase IN ('accepted','prepared','sending','acknowledged','ambiguous','completed','failed')),
 receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(principal_id,request_id)
);
CREATE TABLE workspaces (
 canonical_path TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT REFERENCES attempts(attempt_id),
 executor_id TEXT NOT NULL,
 git_common_dir TEXT NOT NULL,
 base_revision TEXT NOT NULL,
 lease_state TEXT NOT NULL CHECK(lease_state IN ('held','release_pending','released')),
 fencing_token INTEGER NOT NULL CHECK(fencing_token>=1),
 owner_pid INTEGER,
 owner_started_at TEXT,
 input_manifest_artifact TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE approvals (
 approval_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 principal_id TEXT NOT NULL,
 backend_request_id TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
 session_id TEXT NOT NULL,
 action TEXT NOT NULL,
 scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
 scope_sha256 TEXT NOT NULL,
 challenge_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','expired','sent','ambiguous')),
 user_decision_ref TEXT,
 decision_origin TEXT,
 created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 decided_at TEXT,
 UNIQUE(attempt_id,backend_request_id,generation)
);
CREATE TABLE artifacts (
 artifact_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 result_revision INTEGER NOT NULL CHECK(result_revision>=1),
 section TEXT NOT NULL CHECK(section IN ('summary','checks','changes','artifacts','content','input','raw')),
 visibility TEXT NOT NULL CHECK(visibility IN ('private','export')),
 relative_path TEXT NOT NULL,
 media_type TEXT NOT NULL,
 byte_length INTEGER NOT NULL CHECK(byte_length>=0),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 source_sha256 TEXT,
 redacted INTEGER NOT NULL CHECK(redacted IN (0,1)),
 complete INTEGER NOT NULL CHECK(complete IN (0,1)),
 applyable INTEGER NOT NULL CHECK(applyable IN (0,1)),
 created_at TEXT NOT NULL,
 UNIQUE(attempt_id,relative_path),
 CHECK(NOT (redacted=1 AND applyable=1))
);
CREATE TABLE checks (
 check_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 command_json TEXT NOT NULL CHECK(json_valid(command_json)),
 cwd TEXT NOT NULL,
 started_at TEXT NOT NULL,
 ended_at TEXT,
 exit_code INTEGER,
 tool_status TEXT,
 verdict TEXT NOT NULL CHECK(verdict IN ('not_run','passed','failed','unknown','stale')),
 source_snapshot_sha256 TEXT NOT NULL,
 evidence_artifact_id TEXT REFERENCES artifacts(artifact_id)
);
CREATE TABLE state_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT REFERENCES attempts(attempt_id),
 from_state TEXT,
 to_state TEXT NOT NULL,
 reason_code TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 record_version INTEGER NOT NULL CHECK(record_version>=0),
 UNIQUE(job_id,record_version)
);
CREATE TABLE backend_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 attempt_seq INTEGER NOT NULL CHECK(attempt_seq>=1),
 source_event_id TEXT,
 event_kind TEXT NOT NULL,
 session_id TEXT,
 turn_id TEXT,
 observed_at TEXT NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 raw_artifact_id TEXT REFERENCES artifacts(artifact_id),
 UNIQUE(attempt_id,attempt_seq)
);
CREATE TABLE reviews (
 review_id TEXT PRIMARY KEY,
 job_id TEXT REFERENCES jobs(job_id),
 attempt_id TEXT REFERENCES attempts(attempt_id),
 reviewer_id TEXT NOT NULL,
 source_commit_sha TEXT,
 source_snapshot_sha256 TEXT NOT NULL,
 verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL','BLOCKED')),
 report_artifact_id TEXT REFERENCES artifacts(artifact_id),
 created_at TEXT NOT NULL
);
CREATE INDEX jobs_by_principal_created ON jobs(principal_id,created_at DESC,job_id DESC);
CREATE INDEX events_by_attempt ON backend_events(attempt_id,attempt_seq);
CREATE INDEX artifacts_by_attempt_revision ON artifacts(attempt_id,result_revision,artifact_id);
