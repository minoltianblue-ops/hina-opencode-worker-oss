# Offline quickstart

Prerequisites: Python 3.12+ and a POSIX-like shell (macOS is the primary target).

```sh
python3 -m venv .venv
source .venv/bin/activate
PYTHONPATH=src python -m unittest discover -s tests -q
python -m compileall -q src tests experiments
python -m pip install -r requirements-dev.txt
python docs/contracts/validate_spec.py
```

These tests use a local fake backend plus sanitized fixtures: no OpenCode account, tunnel, API key, or real permission changes are required.

For an optional **live** Mac experiment, supply your own OpenCode V2 server, explicit model and separately configured tunnel. Read [M4 operations reference](M4_OPERATIONS.md) and [Security model](SECURITY_MODEL.md), confirm localhost-only listening and maintain an independent runtime backup. No unattended startup is installed by this repository.
