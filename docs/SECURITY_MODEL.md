# Security model

Client messages are inputs, not direct OS authorization. The supervisor, explicit user decisions, per-job capability policy, execution journal and local file-access boundary control actions.

## Trust boundaries

- Optional transport carries a message; it does not grant permissions.
- The MCP contract restricts parsing and identities; a connection alone does not authorize mutations.
- The worker can only act within recorded model, workspace and capability limits.
- Results should remain available after reconnection without creating duplicate dispatch.
- Local credentials and original backend sessions are never test fixtures for public release.

## Release limitation

The v0.1.0 source includes broader primitives than the **scoped** live inspect path verified in the original developer setup. General unattended write or edit access, auto-start, and public-endpoint deployment are not claimed.
