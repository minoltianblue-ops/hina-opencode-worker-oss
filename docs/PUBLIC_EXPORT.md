# Public-export provenance

This repository uses a **new Git history**, not a clone of the private development history. It preserves source, tests, contracts and selected instructions, but omits original review and execution logs, archived investigations, and private runtime profiles.

The `tests/golden` regression corpus was sanitized: personal home paths and real session/turn/part/call identifiers were pseudonymized consistently, and fixture hashes were updated. The original logs remain private. Sanitized fixtures demonstrate only the asserted offline behavior.

No existing private Git repository or running Mac service was altered to create this public source snapshot.
