# Worker MCP 계약 v0.3

**우리 제품의 규격**이다. OpenCode 원시 API 형식과 다르다. 기계 규격은 [mcp-tools.json](mcp-tools.json), 예시는 [examples.json](examples.json). 아직 이 규격을 구현한 MCP 서버가 있다는 뜻은 아니다.

## 1. 전송과 인증

초기 배포 경로는 **Secure MCP Tunnel → stdio MCP proxy → Mac 로컬 supervisor의 Unix domain socket**이다. stdio proxy를 supervisor와 같은 프로세스로 구현하지 않는다. stdin EOF나 MCP request cancellation은 현재 job 취소가 아니다. 8개 도구 discovery/JSON-RPC/버전 협상은 MCP SDK로 처리하고, 지원하지 않는 protocolVersion을 그대로 echo하지 않는다.

Tunnel control-plane runtime key는 기존 방식의 Keychain/환경 참조로 관리하며 CLI 인수·로그·Git에 넣지 않는다. 기존 cokacremote 터널 설정을 덮어쓰지 않고 별도 profile/등록을 사용한다. 실제 tunnel 생성/연결은 M4 승인 범위. Tunnel ID나 client JSON의 user 문자열을 로컬 인증으로 믿지 않는다.

로컬 socket 경로는 `~/Library/Application Support/HinaOpenCodeWorker/run/supervisor.sock`, 부모 0700/socket 0600. proxy와 supervisor가 동일 UID인지 macOS peer credentials(`getpeereid` 또는 검증된 동등 수단)로 확인한다. local principal은 서버가 `local:<uid>`로 부여한다. 클라이언트가 principal을 입력할 필드는 없다. 초기 제품은 단일 사용자이며 multi-tenant 격리를 주장하지 않는다. Tunnel의 workspace/org 접근제어가 실제 적용되는지 M4에서 검증한다.

HTTP transport는 초기 범위에서 제외한다. 채택 변경 시 loopback binding, Origin 검증, 인증, token lifecycle의 ADR와 시험이 선행되어야 한다. API 키로 모델 인증을 proxy에 복사하는 방식은 쓰지 않는다. OpenCode 인증은 OpenCode가 소유한다.

## 2. 공통 입력/응답

모든 입력은 JSON object, 명시하지 않은 속성은 거절한다. 계약 문자열은 `0.3`. request_id는 `req_`+lowercase UUIDv4, job_id `job_`, attempt_id `att_`, artifact_id `art_`, approval_id `apr_`+UUIDv4다. backend session ID는 별도 opaque string이다. `request_id`를 JSON-RPC `id` 또는 session ID와 혼동하지 않는다.

상태 변경 도구 start/followup/cancel/approve는 request_id 필수. 같은 인증 principal+request_id+같은 정규화 payload는 동일한 receipt를 반환하며 prompt·approve·cancel을 다시 전송하지 않는다. 다른 입력은 `IDEMPOTENCY_CONFLICT`. 다른 상태변경 도구가 같은 ID를 사용해도 충돌이다. 상태조회 도구는 read-only로 request_id를 요구하지 않는다.

성공 응답은 `{contract_version, tool, observed_at, receipt, data}`. 실패 응답은 `{contract_version, tool, observed_at, receipt, error}`. data/error는 둘 중 하나만. `receipt.request_id`는 조회에서 null, `replayed`는 재전송 여부다. MCP result의 structuredContent가 이 객체이고, content의 TextContent에도 같은 JSON을 넣는다. isError는 **요청 자체 실패**에만 true. 작업 조회 결과가 failed라고 조회 요청까지 실패 처리하지 않는다.

각 도구의 required/type/default/max는 JSON Schema 참조. start는 disk에 accepted request/job가 commit된 후 즉시 반환하며 모델 첫 응답을 기다리지 않는다. 목표는 정상 로컬 상태에서 2초 안에 접수, 5초 안에 응답 또는 명확한 접수 오류다. 장기 OpenCode 실행과 수락 응답을 한 요청에 묶지 않는다.

`user_request_ref`/`user_decision_ref`는 사용자의 해당 지시를 찾아볼 감사용 참조다. 그 문자열이나 모델이 보낸 approved=true는 실제 사용자 의사의 암호학적 증거가 아니다. 인증된 ChatGPT 경로와 사용자 확인 UI를 결합하고, 검증되지 않은 승인 채널은 `APPROVAL_CHANNEL_UNAVAILABLE`로 막는다.

## 3. 도구별 의미

| 도구 | 필수 입력 | data | 추가 의미 |
|---|---|---|---|
| worker_catalog | 없음 | engine, models[], profiles[], capabilities, gate | refresh는 설정/상태 조회만; LLM 호출 없음 |
| worker_start | request_id, user_request_ref, goal, workspace, profile, model, constraints, done_when | job | model은 카탈로그에서 허용된 명시 ID. 빈 기본 모델 추정 금지 |
| worker_status | job_id 또는 목록 필터 | jobs[], next_cursor, snapshot_id | 단일 조회도 jobs 배열. 둘을 섞으면 INVALID_ARGUMENT |
| worker_wait | job_id | job, wait_expired | 0–25000ms, 기본 20000. timeout은 정상 반환이며 job 지속 |
| worker_result | job_id | job, result_revision, section, items[], next_cursor, truncated | section별 분할. content에는 artifact_id 필수 |
| worker_followup | request_id, user_request_ref, job_id, expected_attempt_id, mode, message | job | continue/retry/select_model. 규칙 아래 |
| worker_cancel | request_id, job_id, expected_attempt_id | job, cancel_requested | 비동기 요청. 실제 확인 전 cancelled 아님 |
| worker_approve | request_id, job_id, attempt_id, approval_id, challenge, decision, user_decision_ref | job, approval_id, decision_recorded | exact pending action 범위, 만료·재사용 검증 |

### Followup / 모델 선택

`continue`: completed 및 backend idle 확정 상태, 같은 model/session의 새 turn. 새 goal/scope를 몰래 확장하지 않는다.
`retry`: failed에서 같은 모델의 재시도. 부분 변경을 요약하고 사용자 지시 근거가 있어야 하며 새 attempt. 원래 prompt의 무조건 재전송 아님.
`select_model`: waiting_model_selection에서만 가능. model과 user_decision_ref 필수, 새 모델을 catalog로 검증, 이전 attempt backend 정지·증거 보존 확인 후 새 attempt. 동일 session에서 모델 변경이 M0로 검증되지 않았다면 새 세션+부분 산출물 인계.

waiting_approval은 모델 변경 수단이 아니다. 해당 동작만 승인/거절한다. busy 상태에서 followup 요청은 JOB_BUSY; 이를 다음 작업 queue로 만들지 않는다. expected_attempt_id가 다르면 STALE_ATTEMPT. 재전송 키 검사는 현재 상태 검증보다 먼저 하여 이미 접수된 followup을 busy라고 오판하지 않는다.

### 프로필

inspect: 원본 읽기 가능, shell 테스트/수정 불가. develop: writer의 별도 Worktree 안에서 수정·테스트. operate는 catalog에서 disabled이고 첫 버전 start schema에서 제외. 자료 종류에 따른 allow/deny는 하지 않는다. 자료 경로/실제 지시·부작용이 범위다. profile enforcement가 M0에서 확인되지 않으면 develop은 CAPABILITY_UNVERIFIED.

## 4. 대기·오류·시간 규칙

wait는 monotonic deadline을 사용하고, mutation lock이나 DB transaction을 잡고 잠들지 않는다. 타 cancel/approve가 1초 안에 접수 가능한지 로컬 부하시험한다. 대기 중 backend poll 예산은 남은 wait 예산 이내이며 느린 HTTP 응답을 기다려 무한 연장하지 않는다. backend 제어 요청 기본 timeout 5초, 별도 명시 없으면 1회; 불확실한 mutation은 자동 재전송 금지.

기본 wait=20초/max25초는 **호출 응답 예산**이고 job 종료시간이 아니다. Tunnel 문서의 120초 기본 deadline은 인프라 상한이지 job 제한이 아니다(R5). wait_expired=true에도 job은 running일 수 있다. 클라이언트가 응답을 못 받으면 같은 request_id로 조회/재전송하여 접수 여부를 확인하고 새 start ID를 만들지 않는다.

| error.code | 의미 | 호출자 행동 |
|---|---|---|
| INVALID_ARGUMENT | schema 또는 의미 검증 실패 | 입력 수정, 새 request_id |
| UNAUTHENTICATED / FORBIDDEN | 로컬 인증/권한 실패 | 자동 재시도하지 않고 접근 확인 |
| NOT_FOUND | 존재하지 않거나 접근 권한 없는 job/artifact | 동일하게 표시, 경로 정보 노출 금지 |
| IDEMPOTENCY_CONFLICT | 같은 ID 다른 payload | 원래 request 확인 |
| BUSY / JOB_BUSY | writer 또는 같은 job 사용 중 | status/wait |
| STALE_ATTEMPT / STALE_RESULT | 시도/결과 revision 불일치 | 현재 snapshot 재조회 |
| RESULT_NOT_READY | 아직 확정된 결과 snapshot 없음 | status/wait 후 재조회 |
| MODEL_NOT_ALLOWED / MODEL_UNAVAILABLE | 설정 또는 현재 모델 사용 불가 | catalog와 사용자 선택 |
| CONTRACT_MISMATCH / CAPABILITY_UNVERIFIED | 검증한 V2와 다름/필수 기능 미검증 | M0 gate 재검증 |
| APPROVAL_REQUIRED / APPROVAL_INVALID / APPROVAL_EXPIRED | 승인 필요/범위·만료 불일치 | 정확한 pending approval 확인 |
| APPROVAL_CHANNEL_UNAVAILABLE | 사용자 확인 전달 경로 미검증 | 기술 blocker 보고 |
| WORKSPACE_DIRTY / WORKSPACE_CHANGED / WORKSPACE_CONFLICT | 입력/lease/기준 변경 | 변경 보존 후 사용자 판단 |
| CURSOR_INVALID / CURSOR_EXPIRED | 결과 페이지 토큰 무효 | 첫 페이지부터 snapshot 재조회 |
| BACKEND_UNAVAILABLE / BACKEND_TIMEOUT | V2 요청 실패/시간 초과 | 실행 불명 여부 포함, 새 prompt 금지 |
| DISPATCH_AMBIGUOUS / CANCEL_UNCONFIRMED | 외부 부작용 여부 확정 안 됨 | needs_attention, lease 유지 |
| EVIDENCE_INCOMPLETE / STORAGE_FULL / INTERNAL | 증거·디스크·내부 실패 | 기존 파일 보존, 신규 접수 보류 |

error는 `{code,message,retryable,details}`이며 details는 범위 제한된 safe 문자열 map이다. stack trace/토큰/원문 명령을 무차별 반환하지 않는다. INVALID_ARGUMENT는 도구 결과 오류로 정규화하고, malformed JSON-RPC/unknown method/tool 등 protocol 오류는 SDK 표준 -32700/-32600/-32601/-32602 경로로 처리한다.

## 5. 페이지네이션·크기

status 목록은 limit=20/max100, created_at+job_id 역순의 안정 snapshot. result 목록은 limit=50/max100, artifact content는 byte_limit=16384/max65536. 성공/실패 전체 직렬화 응답(중복 text 포함)은 최대 256KiB; 입력 envelope 최대 256KiB. 초과는 페이지화 또는 명확한 오류, JSON 중간 절단 금지.

cursor는 base64url(payload)+HMAC-SHA256, payload에 principal, tool, job/attempt, section, filters_hash, snapshot_id/result_revision, next_offset, expires_at 포함. 만료 24시간. cursor HMAC 키는 0600 로컬 파일, 재시작 후 유지. 이 TTL은 데이터 보관기간이 아니다. 잘못된 cursor는 처음부터 조용히 다시 읽지 않는다.

content 페이지는 정확한 byte chunk를 base64로 제공하고 offset/bytes/total_bytes/sha256_chunk/sha256_artifact를 반환한다. 문자열 경계·JSON escaping 전후 혼동 없이 decode된 바이트로 검증한다. 모든 페이지를 모으기 전 적용 가능한 패치라고 표시하지 않는다. preview truncation 여부와 artifact completeness를 별도로 구분한다. backend raw/private 파일은 이 도구로 직접 다운로드하지 않고 마스킹된 export artifact만 제공한다.

## 6. 보안 annotation과 호환성 검사

catalog/status/wait/result는 readOnlyHint=true. 나머지는 false, destructiveHint=true, 사용자 확인이 필요한 tool로 표시. annotation은 보안 경계가 아니라 UI 힌트다(R3). SDK 버전·MCP protocol 협상·stdio framing·8개 tools/list·isError·structuredContent·input/outputSchema를 contract test로 검사한다. 프로토콜 대역 테스트만으로 실제 ChatGPT 연결 PASS를 선언하지 않는다.

## 7. 승인 조회와 재발급의 구체 계약

job snapshot의 `pending_approvals`가 action/scope_summary/scope_sha256/attempt/expiry와 challenge를 전달한다. 인증된 같은 principal만 조회한다. UI 승인 채널 미검증이면 challenge=null, channel_status=unavailable이며 승인 실행을 막는다. challenge는 32-byte HMAC-SHA256의 base64url 값으로, 별도의 `keys/approval.key`와 `(principal,approval_id,attempt_id,backend_request_id,scope_sha256,generation,expires_at)` canonical payload에서 계산한다. 로그/터미널/Git에는 내보내지 않는다. DB는 challenge_sha256만 보존하고 재조회는 key로 동일 값을 재계산한다.

승인 유효기간은 발급 후 15분(기술 토큰 TTL, 작업 제한시간 아님). 만료 시 job=needs_attention, backend pending 여부 재확인. 초기 제품에서는 로컬 관리자 `approval renew <approval_id>`의 명시 요청으로만 generation+1의 새 approval을 만든다(미구현 예정 명령). 자동 승인·모델 전환·부분 변경 삭제 없음. 새 scope면 새 사용자 확인이 필요하다. 이미 접수된 approve의 동일 request replay는 TTL/state 재검사보다 request journal 조회가 먼저다.

start 성공은 job와 첫 attempt intent를 함께 저장하여 active_attempt_id를 반드시 반환한다. 아직 Worktree가 없으면 workspace=null이다. 따라서 시작 직후에도 정확한 attempt에 cancel을 보낼 수 있다. 완료 snapshot이 없을 때 worker_result는 RESULT_NOT_READY; 전체 job 상태는 worker_status로 조회한다.
