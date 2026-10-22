# Security policy

## Experimental status

This project is not a security-certified general remote executor. The tested manual inspect path is narrower than arbitrary editing, deployment, or unattended execution.

## Report a vulnerability

**Do not post exploit details or secrets in a public Issue.** Use GitHub's private vulnerability-reporting form if enabled for this repository, or contact the repository owner privately.

## Deployment rules

- Keep the worker endpoint bound to `127.0.0.1`; do not publish a raw command/MCP socket.
- Require scoped authorization for capability grants, model changes and write operations.
- Keep keys, session stores, SSH files, databases, tunnel profiles and raw logs outside the repository.
- Do not turn a transport retry or client timeout into a new model dispatch.
- Treat observed tunnel health and verified ChatGPT round-trip as separate checks.
