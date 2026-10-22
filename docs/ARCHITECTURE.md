# Architecture

Hina OpenCode Worker splits requests, durable state, execution, policy and result delivery.

1. **MCP proxy / bridge**: validates a bounded set of tool requests and routes them over a separately managed tunnel or local stdio channel.
2. **Local endpoint and supervisor**: accepted jobs have stable identities and a persistent lifecycle independent of a single client connection.
3. **SQLite journal**: `src/hina_opencode_worker/store.py` with `schema.sql` / `states.json` stores idempotent requests, attempts, events and result metadata.
4. **V2 adapter**: `opencode_v2.py` / `events.py` captures structured output while respecting sessions, turns and uncertain completion.
5. **Worktrees and artifacts**: `workspace.py` and `artifacts.py` isolate changes and preserve reviewable results.
6. **Policy modules**: `models.py`, `capabilities.py` and `operate.py` encode authorization and model/capability boundaries.
7. **M4 experiments**: manually supervised Mac transport, guard and inspection scripts, not a one-command installer.

## Invariants

- A lost acknowledgement is ambiguous; reconcile, do not blindly repeat work.
- Neither an idle backend nor a final text fragment alone proves completion.
- An unknown model/capability never silently becomes an approved replacement.
- Network access never implies consent to command execution or filesystem mutation.
- Local-only binding and secret isolation are required for live operation.

[Security model](SECURITY_MODEL.md) | [MCP contract](contracts/MCP.md) | [Roadmap](ROADMAP.md)
