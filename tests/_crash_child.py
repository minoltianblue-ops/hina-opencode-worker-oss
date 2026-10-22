"""Crash-window child process for T03.

Runs the real store/journal against a given DB, advances the request
phase to the requested target, then sleeps until the parent SIGKILLs it.
Committed rows must survive the forced kill (WAL + synchronous=FULL).

usage: python3 _crash_child.py <db> <marker_file> <dispatch_log> <up_to>
  up_to in {prepared, sending, acknowledged}
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hina_opencode_worker.requests import RequestJournal  # noqa: E402
from hina_opencode_worker.store import Store  # noqa: E402

PAYLOAD = {
    "goal": "crash-window synthetic task",
    "profile": "develop",
    "model": "opencode/glm-5.3-flash-free",
    "workspace": {"repository": "/Users/example/fixture", "source_ref": "HEAD"},
}


def main() -> None:
    db, marker_path, dispatch_log, up_to = sys.argv[1:5]
    marker = Path(marker_path)

    def mark(name: str) -> None:
        with marker.open("a", encoding="utf-8") as fh:
            fh.write(name + "\n")
            fh.flush()

    store = Store.open(db)
    journal = RequestJournal(store)
    journal.intake("u1", "req-crash", "worker_start", PAYLOAD)
    mark("accepted")
    journal.mark_prepared("u1", "req-crash")
    mark("prepared")
    if up_to in ("sending", "acknowledged"):
        if journal.claim_dispatch("u1", "req-crash"):
            with Path(dispatch_log).open("a", encoding="utf-8") as fh:
                fh.write("dispatch req-crash\n")
                fh.flush()
            mark("dispatched")
        mark("sending")
    if up_to == "acknowledged":
        journal.mark_acknowledged("u1", "req-crash")
        mark("acknowledged")
    # Wait for the forced kill.
    time.sleep(120)


if __name__ == "__main__":
    main()
