# 저장·상태·재전송 계약 v0.3

기계 규격: [schema.sql](schema.sql), [states.json](states.json). DDL은 명세 스키마이며 아직 제품 DB가 아니다.

## 1. 디렉터리

```text
~/Library/Application Support/HinaOpenCodeWorker/  (0700)
  config.json                  # 비밀값 없는 정책, 승인된 catalog/버전
  jobs.sqlite + -wal + -shm     # 0600, raw 입력은 private 취급
  run/supervisor.sock           # 0600, peer UID 검사
  run/supervisor.lock           # singleton OS lock
  keys/approval.key            # 0600, approval HMAC; cursor key와 분리
  keys/cursor.key              # 0600, Git/터미널/모델에서 제외
  worktrees/<job_id>/<executor_id>/
  jobs/<job_id>/
    attempts/<attempt_id>/
      private/request.json     # 범위 한정 입력, provider 비밀키 없음
      private/stdout.jsonl     # 원문, 모델 reasoning는 export 대상 아님
      private/stderr.txt
      source-manifest.json     # 실제 파일 바이트/유형/모드
      export/summary.json
      export/checks.json
      export/changes.patch
      export/new-files.manifest.json
      export/<artifact_id>.bin
      export/artifacts.manifest.json
    viewer.json                # 창 ID/상태, 실행 PID와 별도
```

상태 DB·원문은 Git에 넣지 않는다. 저장 파일은 `.partial`에 쓰고 flush/fsync→atomic rename→DB가 completed artifact를 참조. checksum/원장에 없는 `.partial`은 재시작에서 미완성으로 표기하고 자동 삭제하지 않는다. 파일 먼저 확정·DB 참조 나중 순서; 고아 파일은 탐지하되 자동 적용하지 않는다.

SQLite WAL, foreign_keys=ON, synchronous=FULL, busy_timeout=5000ms. 단일 supervisor writer와 짧은 BEGIN IMMEDIATE 트랜잭션. 외부 CLI/API/긴 wait를 트랜잭션 안에서 실행하지 않는다. DB migration 전 백업, user_version 확인; 다운그레이드로 새 DB를 읽지 않는다. umask0077을 파일 생성 전 적용한다.

## 2. 세 상태의 분리

- execution: accepted → starting → running → collecting → completed. 분기 waiting_approval, waiting_model_selection, cancel_requested, cancelled, failed, needs_attention.
- verification: not_run / passed / failed / partial / unknown. check가 실제 실행된 source snapshot hash와 연결되어야 한다.
- acceptance: pending / accepted / rejected. 모델의 자기보고나 CLI exit로 자동 accepted 하지 않는다. 최초 제품에서는 로컬 coordinator 검토 기록으로만 변경한다.

completed는 이번 실행 결과를 수집했다는 뜻이지 기능 합격이 아니다. needs_attention은 실행 불명·증거 불완전·backend 응답 유실로, 정지 보장이 아니다. GUI에는 이 차이를 표시한다.

허용 state pair는 states.json이 규범이며 조건은 다음과 같다.

| 전이 | 조건 |
|---|---|
| accepted→starting | durable intent, workspace lease 확보, catalog/contract/권한 검증 |
| starting→running | backend session/turn과 실제 worktree binding 확인 |
| running→collecting | 현재 turn 종료·진행중 도구 없음 확인 |
| collecting→completed | 최종 export 원자 보존, hashes/manifest 기록 완료 |
| starting/running→waiting_model_selection | 모델 한도/인증/가용성 오류 분류+이전 backend 정지 확인. 정지 불명은 needs_attention |
| running→waiting_approval | 실제 backend approval request에 연결된 pending 승인 저장 |
| waiting_approval→running | 정확한 action/scope 승인·backend 응답 검증 |
| waiting_approval→cancel_requested | 거절 시 이번 작업 중단 절차; 거절만으로 cancelled 아님 |
| waiting_model_selection→starting | 인증된 사용자의 select_model, 부분 결과 확정, 새 attempt |
| *→cancel_requested | active job의 idempotent cancel intent 기록. already cancelled는 no-op |
| cancel_requested→cancelled | exact session과 소유한 도구/fixture의 중단 관측, export 보존 |
| needs_attention→기타 | reconcile로 실제 상태 증명. 외부 실행 불명 상태에서 자동 새 prompt 금지 |
| completed/failed→starting | 명시 followup/retry와 새 attempt. 같은 attempt를 재활용하지 않음 |

생성 accepted를 제외하고 모든 전이는 DB CAS(version)와 state_events append를 같은 트랜잭션으로 기록한다. 금지 pair는 INTERNAL invariant failure. 취소·모델변경 뒤 partial artifact는 남긴다. waiting_model_selection의 이전 attempt는 종료 상태로 닫되 작업 job는 대기 상태, attempt별 실제 경과를 분리한다.

## 3. 요청 멱등성과 crash window

requests PK=(principal_id,request_id). tool+입력 정규화 canonical JSON(SHA256, UTF-8, key sort, compact separator, 유한 숫자만; 문자열 Unicode 정규화/trim은 스키마 규칙 외 하지 않음)을 저장한다. defaults와 resolved paths/model/profile은 첫 접수 때 확정 저장하고 replay에 현재 default를 재적용하지 않는다. challenge 원문은 저장하지 않고 검증 후 digest만 canonical payload에 넣는다.

① 요청 검증→BEGIN→existing 요청을 먼저 조회→동일이면 기존 receipt/상태 반환, 다르면 conflict.
② 새로운 job/attempt/intent+request를 같은 transaction으로 commit. 네트워크 I/O 없음.
③ OS/workspace lease 확보→launch 준비 상태 저장→외부 dispatch→정확한 session/turn을 기록.
④ 외부 요청 앞뒤 crash: prepared는 아직 전송 안 된 경우만 재개 가능. sending이거나 응답 유실이면 `ambiguous`로 두고 동일 backend session/turn을 reconcile. 못 증명하면 needs_attention; 재전송 금지.
⑤ followup/approve/cancel도 같은 journal을 사용. request를 "기록됨"만으로 "실행됨"이라 하지 않는다. 요청 결과 유실은 같은 ID로 조회 가능; 실제 외부 실행 exactly-once를 보장한다고 주장하지 않는다.

입력 task/prompt는 private disk에 저장 가능하지만 provider auth 키는 별도 OpenCode 저장소에 있고 복사하지 않는다. 코드에서 임의 모델 auth export를 하지 않는다.

## 4. lease와 시도

OS singleton lock으로 supervisor 중복 실행 차단. workspace lease는 canonical path와 executor, job, attempt를 연결한다. DB version/fencing token을 할당해 이전 supervisor의 stale 응답이 새 상태를 덮어쓰지 못하게 한다. TTL 만료만으로 쓰기 lease를 풀지 않는다. PID만으로 소유권 판단 금지(시작시각·실행 파일·세션 일치 포함).

wait/status/result는 변경 잠금을 오래 잡지 않는다. viewer가 열렸는지는 상태 정보이며 job execution lock과 별개다. supervisor 재시작은 이전 active attempt를 일단 reconciling 사유의 needs_attention으로 읽고 실제 backend와 대조한다. 불명확한 writer는 새 충돌 writer를 막는다.

## 5. hash와 export 파이프라인

순서: 원문 capture → private raw hash → source snapshot hash → 비밀값/불필요 reasoning 제거 → 최종 UTF-8 export bytes → **export hash** → byte pagination → **chunk hash**.

raw_sha256는 원문 저장 근거, source_snapshot_sha256는 실제 수정·검수 파일 묶음의 근거, export_sha256는 사용자가 받는 파일 바이트의 근거다. 서로 대용하지 않는다. export 파일을 마스킹하거나 줄이면 반드시 새 artifact/revision/hash를 만든다. 반대로 원본 해시를 마스킹 후 결과 해시로 재사용하지 않는다.

patch redaction으로 내용이 바뀌면 applyable=false. 완전한 source patch/새 파일은 private 권한 경로로 coordinator가 통합 검토하고, 마스킹된 공유 patch를 원본에 자동 적용하지 않는다. 전체 코드/개인정보를 채팅에 덤프하지 않는다.

checks는 command(승인된 argv/쉘 의미 구분), cwd, started/ended, 관측 exit nullable, tool status, source_snapshot_sha256, evidence artifact, result를 저장한다. check 이후 source가 변하면 passed를 그대로 재사용하지 않고 stale/partial로 둔다. 테스트 개수는 파서로 확인할 수 없으면 unknown이다.

## 6. 보존·resource limit

자동 TTL 삭제나 작업 60분 강제 종료 없음. 디스크 부족이면 신규 접수 거절·STORAGE_FULL, 실행중 자료 최소 기록 보존 및 needs_attention. 실험용 30/60초 fail-safe는 테스트 fixture 범위에서만 사용한다. 사용자 작업의 무출력은 곧 실패/취소가 아니다.

cleanup은 초기 로컬 관리자 명령으로만 한다. execution 종료·pending approval 없음·원본 반영/보관 확인·artifact 무결성·사용자 확인이 선행. 미검수/미병합 dirty Worktree를 `git worktree remove --force`로 지우지 않는다. 리포트·검증 기록은 요청 없이 자동 삭제하지 않는다.

## 7. 추가 불변식

job의 active_attempt_id는 접수 트랜잭션 안에서 첫 attempt를 만든 뒤 설정하며 commit 후 null일 수 없다. 이후 같은 job에 속하는 attempt만 지정한다. 이 관계는 FK 외에 앱 트랜잭션 검증으로 검사한다. status 응답의 model은 active attempt.model에서 얻는다. model 실패로 대기한 이전 attempt는 failed로 닫고 job만 waiting_model_selection에 둔다.

approvals.generation은 같은 backend 요청의 명시 재발급 횟수이며 UNIQUE(attempt_id,backend_request_id,generation). 재발급은 아직 backend pending인지 확인한 후에만 가능하다. 최초 generation=1. challenge는 원문 저장 없이 승인 전용 HMAC key로 재생성한다. key 유실 시 기존 challenge를 임의 승인하지 않고 재발급 절차로 전환한다.
