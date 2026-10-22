"""State machine per docs/contracts/states.json (normative).

Rules encoded here (STORAGE.md §2):
- Allowed transition pairs come from states.json; forbidden pairs are
  INTERNAL invariant failures, never silent no-ops.
- Same-state observational updates are NOT transitions; calling
  require_transition with from == to is a caller error.
- needs_attention cannot auto-dispatch: every exit requires an explicit
  reconcile proof supplied by the caller.
- completed/failed -> starting requires an explicit followup/retry flag
  (a new attempt is created by the caller; attempts are never reused).
- cancelled is terminal for an attempt.
"""
from __future__ import annotations

import json
from pathlib import Path

STATES_PATH = Path(__file__).resolve().parent / "states.json"

NEEDS_ATTENTION = "needs_attention"
CANCELLED = "cancelled"
COMPLETED = "completed"
FAILED = "failed"
STARTING = "starting"


class InvariantFailureError(RuntimeError):
    """Forbidden state pair or contract invariant violated (INTERNAL)."""


def load_states(path: Path = STATES_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    states = set(data["states"])
    if data["initial"] not in states:
        raise InvariantFailureError("states.json initial not in states")
    if not set(data["terminal_for_attempt"]) <= states:
        raise InvariantFailureError("states.json terminal_for_attempt not a subset")
    for frm, targets in data["transitions"].items():
        if frm not in states:
            raise InvariantFailureError(f"states.json unknown from-state {frm!r}")
        for to in targets:
            if to not in states:
                raise InvariantFailureError(f"states.json unknown to-state {to!r}")
            if to == frm:
                raise InvariantFailureError(f"states.json self-loop {frm!r}")
    if set(data["transitions"]) != states:
        raise InvariantFailureError("states.json transitions must cover every state")
    return data


class StateMachine:
    def __init__(self, data: dict | None = None) -> None:
        self._data = data or load_states()
        self.states = set(self._data["states"])
        self.initial = self._data["initial"]
        self.terminal_for_attempt = set(self._data["terminal_for_attempt"])
        self._allowed = {
            frm: set(targets) for frm, targets in self._data["transitions"].items()
        }

    def is_transition(self, frm: str, to: str) -> bool:
        """Same-state observational updates are not transitions."""
        return frm != to

    def is_allowed(self, frm: str, to: str) -> bool:
        return to in self._allowed.get(frm, set())

    def require_transition(self, frm: str, to: str) -> None:
        """Raise InvariantFailureError unless frm->to is a legal transition."""
        if frm not in self.states or to not in self.states:
            raise InvariantFailureError(f"unknown state pair {frm!r}->{to!r}")
        if frm == to:
            raise InvariantFailureError(
                f"same-state update {frm!r} is observational, not a transition"
            )
        if not self.is_allowed(frm, to):
            raise InvariantFailureError(f"forbidden transition {frm!r}->{to!r}")

    def require_reconcile_proof(self, frm: str, to: str, proof: str | None) -> None:
        """needs_attention exits require proof of the real state (no auto-dispatch)."""
        if frm == NEEDS_ATTENTION and not (proof and proof.strip()):
            raise InvariantFailureError(
                f"needs_attention->{to!r} requires a reconcile proof; "
                "needs_attention cannot auto-dispatch"
            )

    def require_followup(self, frm: str, to: str, explicit_followup: bool) -> None:
        """completed/failed -> starting only via explicit followup/retry."""
        if frm in (COMPLETED, FAILED) and to == STARTING and not explicit_followup:
            raise InvariantFailureError(
                f"{frm}->{to} requires an explicit followup/retry with a new attempt"
            )

    def is_terminal_for_attempt(self, state: str) -> bool:
        return state in self.terminal_for_attempt
