"""Contract drift guard: the runtime copies of schema.sql and states.json
must stay byte-identical to docs/contracts/ until the coordinator revises
both together (PLAN.md §3: implementers must not pick unilaterally)."""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from hina_opencode_worker import state as state_mod
from hina_opencode_worker import store as store_mod

ROOT = Path(__file__).resolve().parent.parent


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class ContractDrift(unittest.TestCase):
    def test_schema_sql_identical(self):
        self.assertEqual(
            _sha(Path(store_mod.SCHEMA_PATH)),
            _sha(ROOT / "docs" / "contracts" / "schema.sql"),
            "src schema.sql drifted from docs/contracts/schema.sql",
        )

    def test_states_json_identical(self):
        self.assertEqual(
            _sha(state_mod.STATES_PATH),
            _sha(ROOT / "docs" / "contracts" / "states.json"),
            "src states.json drifted from docs/contracts/states.json",
        )

    def test_states_self_consistent(self):
        data = state_mod.load_states()
        self.assertEqual(data["contract_version"], "0.3")
        self.assertEqual(data["initial"], "accepted")
        self.assertEqual(data["transitions"]["cancelled"], [])
        self.assertNotIn("needs_attention", data["terminal_for_attempt"])

    def test_installed_schema_accepts_contract_ddl(self):
        # The DDL loads and its CHECKs bind: bad profile must be rejected.
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.executescript(Path(store_mod.SCHEMA_PATH).read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (job_id,principal_id,goal,requested_model,profile,"
                "repository_path,state,created_at,updated_at) VALUES"
                "('j','p','g','m','WRONG','/r','accepted','t','t')"
            )
        conn.close()


if __name__ == "__main__":
    unittest.main()
