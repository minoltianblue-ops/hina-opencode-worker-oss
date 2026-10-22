"""Terminal observer (viewer) — read-only window onto the supervisor.

Connects to the supervisor UDS as client ``viewer``, which the
supervisor restricts to viewer.snapshot / viewer.tail / viewer.requests
(any mutation is FORBIDDEN server-side). The viewer renders the current
job table and a tail of state_events.

Pending followup deliveries (W6b): an explicit steer/queue injection
that has not reached a terminal request phase is observable read-only
via viewer.requests — metadata rows from the request journal, no message
bodies, no challenge material. The viewer never injects, absorbs or
resolves these rows; settling an accepted-phase row is supervisor-side
reconcile only (no auto-queue, no viewer-side retry).

Killing the viewer (Ctrl-C, terminal close, tunnel drop) detaches one
socket only — the supervisor process and the current job are untouched
(T15). Challenge material is stripped server-side before it reaches us
(§7) and every printed line is passed through redact_text as a second
belt.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time

try:  # package import
    from .artifacts import redact_text
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from hina_opencode_worker.artifacts import redact_text

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def call(sock_path: str, method: str, arguments: dict | None = None,
         timeout: float = 10.0) -> dict:
    """One viewer request per connection (same framing as the proxy)."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(sock_path)
        payload = json.dumps({"client": "viewer", "method": method,
                              "arguments": arguments or {}}).encode("utf-8")
        conn.sendall(struct.pack(">I", len(payload)) + payload)
        hdr = _recv_exact(conn, 4)
        if hdr is None:
            raise OSError("viewer: no response header")
        (length,) = struct.unpack(">I", hdr)
        if length > MAX_RESPONSE_BYTES:
            raise OSError("viewer: response too large")
        body = _recv_exact(conn, length)
        if body is None:
            raise OSError("viewer: truncated response")
        return json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def _safe(text: str) -> str:
    return redact_text(text)[0]


def render_snapshot(snap: dict) -> str:
    lines = [f"current_job: {snap.get('current_job') or '(idle)'}",
             f"next_seq: {snap.get('next_seq')}", ""]
    if not snap.get("jobs"):
        lines.append("(no jobs)")
    for j in snap.get("jobs", []):
        appr = j.get("pending_approvals") or []
        appr_note = f" approvals={len(appr)}" if appr else ""
        lines.append(_safe(
            f"{j['job_id']}  {j['state']:<24} "
            f"model={j.get('model')} rev={j.get('result_revision')}"
            f"{appr_note}"
            + (f"  attention={j['attention_reason']}"
               if j.get("attention_reason") else "")))
    return "\n".join(lines)


def render_events(events: list[dict]) -> str:
    out = []
    for e in events:
        out.append(_safe(
            f"[{e.get('observed_at')}] seq={e.get('seq')} "
            f"{e.get('job_id')} {e.get('from_state')} -> "
            f"{e.get('to_state')}"
            + (f" ({e['reason_code']})" if e.get("reason_code") else "")))
    return "\n".join(out)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--socket", required=True, help="supervisor UDS path")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", default=True,
                      help="print one snapshot and exit (default)")
    mode.add_argument("--watch", action="store_true",
                      help="poll state_events and print transitions")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json", action="store_true",
                    help="emit raw JSON envelopes instead of a table")
    ns = ap.parse_args(argv)

    sock = os.path.expanduser(ns.socket)
    try:
        snap = call(sock, "viewer.snapshot")
    except OSError as e:
        sys.stderr.write(_safe(f"viewer: supervisor unreachable: {e}\n"))
        return 1
    if "error" in snap:
        sys.stderr.write(_safe(f"viewer: refused: "
                               f"{snap['error'].get('code')}: "
                               f"{snap['error'].get('message')}\n"))
        return 1
    if ns.json:
        print(json.dumps(snap, ensure_ascii=False))
    else:
        print(render_snapshot(snap.get("data", {})))
    if not ns.watch:
        return 0

    after = snap.get("next_seq", 0)
    try:
        while True:
            time.sleep(ns.interval)
            tail_env = call(sock, "viewer.tail", {"after_seq": after})
            tail = tail_env.get("data", {})
            events = tail.get("events") or []
            if events:
                if ns.json:
                    print(json.dumps(tail, ensure_ascii=False))
                else:
                    print(render_events(events))
                    print(render_snapshot(tail))
                sys.stdout.flush()
            after = tail.get("next_seq", after)
    except KeyboardInterrupt:
        # Viewer detach is harmless; the job keeps running (T15).
        sys.stderr.write("viewer: detached (job unaffected)\n")
        return 0
    except OSError as e:
        sys.stderr.write(_safe(f"viewer: connection lost: {e}\n"))
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
