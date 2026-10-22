# Hina OpenCode Worker

**A persistent, approval-aware bridge between ChatGPT and OpenCode V2, designed for macOS.**

Hina OpenCode Worker separates a chat connection's lifetime from an authorized local task's lifetime. A local supervisor, SQLite journal, MCP interface, and OpenCode adapter can retain progress and results when a client disconnects.

**Status: experimental v0.1.0.** The manually managed *inspect* path has been validated on its original Mac setup, but this is **not** a general-purpose remote shell, a consumer installer, or a claim that unattended edit/develop operations are production-ready.

## Features

- Persistent job/attempt state and artifacts, reconnection support and idempotent request handling.
- Explicit, recorded approval for model selection and capabilities; no silent fallback or global privilege escalation.
- Git worktree isolation, partial-result preservation, and independent review design.
- Local-only transport endpoint with an optional separately managed secure tunnel.
- Offline regressions, documented MCP contracts, and sanitized OpenCode event fixtures.

```text
ChatGPT/client --[optional secure tunnel]--> stdio MCP bridge
                                                |
                                      localhost-only endpoint
                                                |
                              Supervisor + SQLite journal/artifacts
                                                |
                              OpenCode V2 adapter -> user's backend/model
```

[Architecture](docs/ARCHITECTURE.md) | [Security](docs/SECURITY_MODEL.md) | [MCP contract](docs/contracts/MCP.md)

## Current implementation boundaries

| Component | Status |
|---|---|
| Supervisor, journal, artifacts, MCP contract, worktree primitives | Implemented; offline tested |
| Tunnel guard, endpoint health/readiness | Implemented; verified in the original macOS setup |
| Manually managed ChatGPT-to-worker `inspect` | Scoped end-to-end validation on the original Mac |
| Model/capability policy modules | Unit tested, but not claimed fully wired into every live operation |
| General unattended `develop`, auto-start and cross-platform installer | **Not** supported release claims |

Offline success is not evidence that a new machine's tunnel or provider configuration is secure.

## Offline quickstart (no credentials required)

Python **3.12+**; no mandatory third-party runtime packages.

```sh
git clone https://github.com/minoltianblue-ops/hina-opencode-worker-oss.git
cd hina-opencode-worker-oss
python3 -m venv .venv
source .venv/bin/activate
PYTHONPATH=src python -m unittest discover -s tests -q
python -m compileall -q src tests experiments
```

Optional contract checks:

```sh
python -m pip install -r requirements-dev.txt
python docs/contracts/validate_spec.py
```

No provider token, tunnel, or running OpenCode process is used in this offline path. See [Quickstart](docs/QUICKSTART.md) before exploring the Mac-specific operator scripts.

## Privacy and safety

Do not expose the local worker endpoint publicly or commit API keys, tunnel settings, raw user prompts, or runtime databases. This public repo has a **clean Git history** and **redacted/pseudonymized** fixtures. Original operational logs and historical reviews are not distributed.

Read [SECURITY.md](SECURITY.md) and [Export notes](docs/PUBLIC_EXPORT.md).

## Contributing

Pull requests, Issues, reproducible regressions, documentation and security review are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Roadmap](docs/ROADMAP.md).

**License:** [MIT](LICENSE), © 2026 minoltianblue-ops.

Independent community project; not affiliated with or endorsed by OpenAI or OpenCode maintainers.

---

**한국어:** Hina OpenCode Worker는 ChatGPT 대화 연결과 로컬 작업 수명을 분리하는 macOS 중심 실험적 오픈소스입니다. 승인 기반 실행, 모델 선택, 실행 원장, 재접속 및 결과 보존을 다루며, 실제 검증 범위와 미완료 기능을 명확히 구분합니다.
