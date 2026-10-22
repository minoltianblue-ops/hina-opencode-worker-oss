# OpenCode V2 외부 계약과 어댑터 경계

**두 종류를 분리한다.** 아래 `OBSERVED_HELP`는 2026-10-18 Mac에서 확인된 CLI 옵션을 설명하되 공개 fixture는 비식별화된 자료다. `OPEN_LIVE`는 실증 전 외부 계약이다. 내부 정규화 모델은 우리 설계이며 OpenCode가 그 JSON을 반환한다는 뜻이 아니다.

## 1. 관측된 실행 옵션

The original live-evidence files are retained privately. The public repository bundles sanitized offline regression fixtures under tests/golden/.

- 절대 binary: `/Users/example/.opencode/bin/opencode`; 확인 버전 `opencode v2.0.30`.
- `run --model <provider/model#variant> --format json --title <title> <message>`.
- `run --session <id>`: 도움말상 continue/create. 우리가 임의 ID를 만들 수 있는지, 기존 세션의 위치/모델을 유지하는지는 별도 실증.
- `run --auto`: explicitly denied를 제외한 권한 자동 승인; 제품의 전역 기본값이 아님.
- `api <operation | method path...>` 및 `--data`, `--header`, `--param` 존재. 이것만으로 특정 인증/operation 동작이 입증되지 않음.
- `models`는 이 버전 도움말상 provider positional 인수를 받지 않음. V1 `--variant`, `--pure`, `models <provider>`를 하드코딩하지 않음.

문서 링크는 참고다. 다른 버전 또는 `/docs/cli`의 V1 예시와 `/v2/docs`를 혼합하지 않는다.

## 2. 외부 계약 매핑표 — W0가 채우고 R0가 승인

| contract key | 현재 상태 | 반드시 관측할 것 | gate |
|---|---|---|---|
| CLI-ARGV | OBSERVED_HELP | 사용 가능한 인수·실행 binary/hash | 00 |
| EVT-ENVELOPE | OPEN_LIVE | JSONL framing, type, part의 실제 타입/필수성, unknown 처리 | 01/04 |
| SESSION-ID | OPEN_LIVE | sessionID 위치·형식, 명령과 세션의 유일 대응, 서버 identity | 01/08 |
| TURN-ID | OPEN_LIVE | message/turn 경계, 이전 결과와 새 turn 구별 | 08 |
| LOCATION | OPEN_LIVE | cwd/PWD/tool cwd/backend location 일치 관측 | 02 |
| TOOL-RESULT | OPEN_LIVE | shell/read/edit/create, call ID, status, input/output·exit 관측 | 03/04 |
| NORMAL-END | OPEN_LIVE | final 답변, backend idle, pending tool 없음, CLI exit와의 관계 | 01/03/04 |
| PERMISSION | OPEN_LIVE | pending 요청 ID·scope, 승인/거절 API, 응답 확인 | 05 |
| INTERRUPT | OPEN_LIVE | session-only interrupt argv/response, queued prompt/child 동작 | 06 |
| RECONCILE | OPEN_LIVE | start 응답 유실/CLI 사망/새 client에서 해당 세션 상태 확인 | 07/09 |
| PROVIDER-ERROR | OPEN_LIVE | structured quota/auth/not-found와 기타 오류 분리 | 04/10 |
| USAGE | OPEN_LIVE | provider-reported token/cost, 누락·이중집계 구별 | 01/03 |

샘플 raw JSON은 이번 개정에서 만들지 않았다. 과거 대화의 type 이름을 기억해 만든 fixture를 실제 출력이라고 표시하지 않는다. 새 시험 샘플을 위 표에 파일·행과 함께 연결한다.

## 3. 내부 어댑터 인터페이스 — 설계 계약

| 메서드 | 입력 | 반환/예외 의미 |
|---|---|---|
| `probe()` | binary·context | fingerprint, observed version, verified mapping revision, capabilities. 모델 추론 없음 |
| `launch(attempt)` | 확정 workspace/PWD/model/profile/prompt·request fingerprint | launch handle+정확히 대응하는 session/turn binding. 불명확하면 DispatchAmbiguous |
| `observe(binding, cursor, budget_ms)` | backend identity/session/turn | bounded normalized event batch + cursor. 상태 조회는 새 prompt 없음 |
| `interrupt(binding)` | 우리 시도의 exact session/turn | requested / confirmed / unknown. shared server kill 금지 |
| `reply_permission(binding, decision)` | backend permission ID·검증된 승인 | response + 관측 evidence; 전역 approval config 수정 없음 |
| `reconcile(binding)` | 불변 identity+시도 정보 | running/idle/pending/missing/unknown; idle만으로 완료 아님 |
| `followup(binding, task)` | idle 확정 세션, 새 attempt | 같은 세션 새 turn. 전역 --continue 금지 |

binding의 최소 필드는 `server_identity`, `session_id`, `turn_id`(미지원이면 식별 방법+검수된 대안), `workspace_realpath`, `attempt_id`, `execution_owner`다. 서버/세션을 제목 검색만으로 하나 골라 추정하지 않는다.

정규화 event 필드: `attempt_id`, `seq`(관측 순번), `observed_at`, `kind`, `source_kind`, `source_event_id` nullable, `session_id` nullable, `turn_id` nullable, `raw_artifact_id`, `payload`.
`kind`: session_bound, tool_started, tool_finished, message, permission_requested, provider_error, execution_idle, usage, transport_error, unknown.

event 자체는 성공 판정이 아니다. 중복 source ID가 있으면 동일 payload만 dedupe; source ID가 없으면 내용 hash만으로 서로 다른 실제 호출을 합치지 않는다. usage는 provider 보고값으로 표시하고 불완전하면 unknown이다.

## 4. 완료/오류/취소 규칙

완료는 **이번 turn의 확정된 실행 종료 + 진행 중 도구 없음 + 결과 수집 완료**. 최종 검증 통과와는 별개다. CLI exit0이면서 모델/검사 실패가 있을 수 있다. missing exit는 unknown, 거짓 0 생성 금지. `step_finish` 없는 정상 종료가 실제 있다면 그 관측 계약을 채택한다.

provider 한도·인증·모델 불가가 structured 또는 검수된 분류 근거로 관측되면, 기존 실행이 멈췄음을 먼저 확인한 뒤 `waiting_model_selection`. 통신 장애·실행 여부 불명은 `needs_attention`; 코드 테스트 실패는 verification failed이며 자동 모델 선택 상태로 바꾸지 않는다.

중단 요청 성공 HTTP 200만으로 cancelled를 확정하지 않는다. exact session의 activity/pending 상태와 M0 heartbeat/도구 정지를 관측한다. 확인 실패는 `needs_attention`이며 workspace lease 유지. CLI만 죽이거나 다른 세션까지 멈추는 경로는 금지.

## 5. 버전 pin과 변경

mapping에는 binary sha256와 CLI version, 관련 config의 비밀값 제외 fingerprint를 함께 pin한다. 실행 전 fingerprint가 다르면 `CONTRACT_MISMATCH`로 접수 후 실행을 막고 M0 재검증을 요구한다. 자동 업그레이드·V1 대체·옵션 자동 삭제는 하지 않는다. `opencode api`는 인증정보를 반환할 수 있으므로 선택된 session 응답만 수집하며 credentials export는 금지한다.

## 출처

아래 공식 문서는 2026-10-18 확인한 참고 계약이며 로컬 v2.0.30 실증을 대신하지 않는다.

- [R1 OpenCode V2 CLI](https://dev.opencode.ai/v2/docs/cli/commands/)
- [R2 OpenCode V2 API](https://dev.opencode.ai/v2/docs/api) — interrupt 후보; 실제 argv/응답은 OPEN.
- [R3 MCP 도구](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [R4 MCP 전송](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [R5 Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [R6 Goofy1 소스](https://github.com/goofy1goober4/opencode-worker/blob/639a75c69eb1ab3899e7dee6ec863b8f2b0f0279/lib/worker.mjs)
- [R7 Shashank 관리자](https://github.com/shashank-sn/codex-opencode-workers/blob/fc4b98a4984bde6eda5fe2076c68902c70d6c3bb/server/src/job-service.mjs)
- [R8 karais89](https://github.com/karais89/opencode-worker)
- [R9 3dl-dev](https://github.com/3dl-dev/opencode-worker)
- [R10 ManuOtel](https://github.com/ManuOtel/opencode-mcp-bridge)
