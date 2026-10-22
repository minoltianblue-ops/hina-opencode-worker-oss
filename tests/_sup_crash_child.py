"""N3 supervisor crash-path child (U-R1..R3).

Runs a REAL Supervisor + FakeBackend (synthetic, fake-only: no live
backend, no external change) against a shared root, advances the journal
to a committed crash phase, then waits for the parent's SIGKILL. The
phase commits are real (WAL + synchronous=FULL), so durability after the
forced kill is genuine, not simulated — same discipline as T03's
_crash_child, one level up at the supervisor.

usage: python3 _sup_crash_child.py <root> <repo> <up_to> <marker_file>
  up_to in {prepared, sending, acked, collecting, followup-prepared}

Prints one JSON line to stdout (early, flushed):
  {"request_id":..., "job_id":..., "attempt_id":..., "followup_request_id":...}

The deterministic phase hooks below are the test's synthetic kill
windows: they widen exactly one narrow committed window so the SIGKILL
lands inside it. They fake TIMING only — the journal/store/state machine
remain the production code paths.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from _p4_support import (  # noqa: E402
    FakeBackend, ModelCatalog, outcome_completed, rid)
from hina_opencode_worker.supervisor import Supervisor, SupervisorConfig  # noqa: E402

STALL_S = 600
PRINCIPAL = f"local:{os.getuid()}"


def job_state(sup, job_id):
    conn = sup._read()
    try:
        row = conn.execute("SELECT state FROM jobs WHERE job_id = ?",
                           (job_id,)).fetchone()
        return row["state"] if row else None
    finally:
        conn.close()


class Child:
    def __init__(self, root, repo, marker_path):
        self.repo = repo
        self.marker = Path(marker_path)
        self.backend = FakeBackend()
        self.catalog = ModelCatalog(profiles=("inspect", "develop"),
                                    gate="PASS",
                                    capabilities={"develop": True})
        self.catalog.add("fake/free", "free", probe_status="live_verified")
        self.config = SupervisorConfig(root=str(root),
                                       develop_enforcement_verified=True,
                                       approval_channel_verified=False,
                                       wait_poll_s=0.05)
        self.sup = None  # built in run() so hooks can wrap the instance

    def mark(self, name):
        with self.marker.open("a", encoding="utf-8") as fh:
            fh.write(name + "\n")
            fh.flush()

    def start_args(self, request_id):
        return {"request_id": request_id,
                "user_request_ref": "req/2026/crash-0001",
                "goal": "N3 synthetic crash-path task",
                "workspace": {"repository": str(self.repo),
                              "source_ref": "HEAD"},
                "profile": "inspect", "model": "fake/free",
                "constraints": [], "done_when": ["answer given"]}

    def build_sup(self):
        self.sup = Supervisor(self.config, backend=self.backend,
                              catalog=self.catalog)

    def emit(self, payload):
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    def run_start(self, request_id):
        env = self.sup.handle("worker_start", self.start_args(request_id),
                              principal=PRINCIPAL)
        assert "data" in env, env
        return env["data"]["job"]

    def wait_completed(self, job_id):
        for _ in range(400):
            if job_state(self.sup, job_id) == "completed":
                return
            time.sleep(0.05)
        raise SystemExit("job never completed")

    def stall_before_claim(self, marker_name="prepared"):
        """Replace journal.claim_dispatch with a marker + stall wrapper:
        mark_prepared/followup intake has ALREADY committed; the kill
        lands at a committed, never-claimed 'prepared'."""
        orig = self.sup.journal.claim_dispatch

        def hooked(principal, request_id):
            self.mark(marker_name)
            time.sleep(STALL_S)
            return orig(principal, request_id)

        self.sup.journal.claim_dispatch = hooked

    def run(self, up_to):
        request_id = rid()
        followup_rid = None

        if up_to == "prepared":
            self.build_sup()
            self.stall_before_claim()
            job = self.run_start(request_id)

        elif up_to == "sending":
            self.build_sup()
            orig_avail = self.sup.backend.available

            def avail_at_sending(model):
                ok = orig_avail(model)
                # claim_dispatch already committed 'sending'; stall
                # before the starting CAS / mark_acknowledged.
                self.mark("sending")
                time.sleep(STALL_S)
                return ok

            self.sup.backend.available = avail_at_sending
            job = self.run_start(request_id)

        elif up_to == "acked":
            child = self

            class AckedFake(FakeBackend):
                def start(self, **kw):
                    run = super().start(**kw)
                    # mark_acknowledged committed just before
                    # backend.start; the session is never persisted.
                    child.mark("acked")
                    time.sleep(STALL_S)
                    return run

            self.backend = AckedFake()
            self.build_sup()
            job = self.run_start(request_id)

        elif up_to == "collecting":
            self.build_sup()
            self.backend.steps = [outcome_completed()]
            store = self.sup._artifact_store
            orig_save = store.save_private

            def save_then_stall(**kw):
                rec = orig_save(**kw)
                # job 'collecting' and request 'acknowledged' are already
                # committed, and this first private artifact (file +
                # registry row) is committed. Stall mid-collection.
                self.mark("collecting")
                time.sleep(STALL_S)
                return rec

            store.save_private = save_then_stall
            job = self.run_start(request_id)

        elif up_to == "followup-prepared":
            self.build_sup()
            self.backend.steps = [outcome_completed()]
            job = self.run_start(request_id)
            self.wait_completed(job["job_id"])
            att1 = job["active_attempt_id"]
            followup_rid = rid()
            # the start's claim already ran; from here the next claim
            # call can only be the followup runner's.
            self.stall_before_claim("followup-prepared")
            env = self.sup.handle("worker_followup", {
                "request_id": followup_rid,
                "user_request_ref": "req/2026/crash-fu-0001",
                "job_id": job["job_id"], "expected_attempt_id": att1,
                "mode": "continue", "message": "synthetic followup"},
                principal=PRINCIPAL)
            assert "data" in env, env
            job = env["data"]["job"]
        else:
            raise SystemExit(f"unknown up_to {up_to!r}")

        self.emit({"request_id": request_id,
                   "job_id": job["job_id"],
                   "attempt_id": job.get("active_attempt_id"),
                   "followup_request_id": followup_rid})
        time.sleep(STALL_S)  # wait for SIGKILL


def main():
    root, repo, up_to, marker = sys.argv[1:5]
    Child(root, repo, marker).run(up_to)


# ---------------------------------------------------------------------------
# parent-side helper used by the U-R tests
# ---------------------------------------------------------------------------

def crash_to(root, repo, up_to, wait_dir):
    """Spawn the child against root/repo, wait for the committed phase
    marker, SIGKILL it, and return the ids it emitted pre-kill. The kill
    is real: only what the child committed (WAL, synchronous=FULL) is
    visible to the restarted supervisor."""
    wait_dir = Path(wait_dir)
    marker = wait_dir / f"marker-{up_to}"
    out = wait_dir / f"child-{up_to}.out"
    err = wait_dir / f"child-{up_to}.err"
    with out.open("wb") as fo, err.open("wb") as fe:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             str(root), str(repo), up_to, str(marker)],
            stdout=fo, stderr=fe)
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            if marker.exists() and up_to in marker.read_text().split():
                break
            if proc.poll() is not None:
                proc.wait()
                raise AssertionError(
                    f"child died early rc={proc.returncode}: "
                    f"{err.read_text(errors='replace')}")
            time.sleep(0.05)
        else:
            proc.kill()
            proc.wait()
            raise AssertionError(
                f"child never reached {up_to!r}: "
                f"{err.read_text(errors='replace')}")
        # settle: let the child's main thread reach its flushed emit
        # (the marker comes from a runner-thread hook) before the kill.
        time.sleep(0.3)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines, f"child emitted no ids: {err.read_text(errors='replace')}"
    return json.loads(lines[-1])


if __name__ == "__main__":
    main()
