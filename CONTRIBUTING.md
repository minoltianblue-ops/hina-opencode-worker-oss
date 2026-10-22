# Contributing

Thank you for helping improve Hina OpenCode Worker. Focused pull requests, Issues, docs and security regressions are welcome.

1. Open an Issue for significant API or behavior changes and discuss permission implications.
2. Use a feature branch/worktree, and include reproduction steps, relevant tests, and any known limitations.
3. Run the offline unit tests and contract validation commands from README.
4. Never commit real tokens, private prompts, personal workstation paths, server profiles, or raw logs.
5. Tests must not call real providers, deploy a LaunchAgent, or reconfigure an existing OpenCode server.
6. Preserve explicit user approvals: no implicit model switch, privilege grant, or duplicate dispatch after a timeout.
7. Keep mutations isolated and reviewable; tests passing locally must not be advertised as live certification.

By contributing, you agree to license your contribution under this repository's MIT license.
