# M4 운영 참고자료 — Mac mini, 수동 관리 inspect

> 공개 소스와 함께 제공하는 참고 절차입니다. 자동 설치 스크립트가 아니며,
> 기존 운영자의 비공개 검증기록은 포함하지 않습니다. 실제 서비스 변경 전
> 해당 환경에서 별도 검증 및 승인이 필요합니다.

이 문서는 승인된 **읽기 전용 inspect / 수동 관리 실행** 범위의 운영 절차다.
현재 배치 사실과 판정은 `M4_CLOSURE_20261021.md` 및 런타임의
`run/active-release.json`을 함께 확인한다. 재부팅·로그아웃 자동 시작, develop,
외부 MCP/skill 런타임 배선, 공개 포트 개방은 완료 범위에 포함하지 않는다.

## 구성과 신뢰 경계

`ChatGPT A3 커넥터 → 기존 전용 Tunnel → stdio bridge → 127.0.0.1:8899 endpoint
→ 로컬 supervisor → 이미 실행 중인 OpenCode 서버 → 사용자 선택 모델`.

- 런타임 루트: `~/Library/Application Support/HinaOpenCodeWorker`.
- 프로필: `~/.config/tunnel-client/hina-worker.yaml`, health/readiness `127.0.0.1:8081`.
- 현재 SUT: `opencode-go/muse-spark-1.3-contributor`. 모델 자동 fallback 없음.
- OpenCode 서버 및 cokacremote 터널은 다른 구성 요소이며 여기서 재기동하지 않는다.
- 코드 SHA, 실제 배치 파일 해시, DB 백업 경로는 release/deployment 증거로 따로 기록한다.
- API 키는 기존 Keychain helper가 자식 환경에 전달한다. 키를 명령행·문서·Git에 넣지 않는다.

## 점검

`run/active-release.json`에서 실제 `release_dir`와 `guard_spec`를 읽는다.
옛 보고서의 PID나 worktree 경로를 현재 운영값으로 재사용하지 않는다.

```sh
ROOT="$HOME/Library/Application Support/HinaOpenCodeWorker"
PY=$(command -v python3)
RELEASE="$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_dir"])' "$ROOT/run/active-release.json")"
"$PY" "$RELEASE/experiments/m4/a3/transport_probe.py"
"$PY" "$RELEASE/experiments/m4/a3/run_stack.py" --root "$ROOT" status
```

`transport_probe`는 실제 bridge의 열린 stdin으로 initialize/ping/tools/list/catalog를
검사한다. `/healthz`와 `/readyz`의 200을 별도로 확인한다. **로컬 PASS는 외부
ChatGPT 왕복 PASS가 아니다.** 외부 경로는 A3 `worker_catalog` 및 기존 job의
`worker_status/worker_result`로 확인한다. 점검 때문에 새 작업을 만들지 않는다.

## 기동 및 중지

기동은 승인된 운영자만 수행한다. 먼저 active-release 파일의 배치 SHA·파일 해시,
기존 프로세스 소유권, 고정 포트 점유, 진행 중 작업 여부를 확인한다.

1. 사용자가 관리하는 기존 OpenCode 서버가 정상이며, supervisor/endpoint/guard가
   중복 실행되지 않는지 확인한다. 기존 서비스가 있으면 새로 실행하지 않는다.
2. 아래 명령으로 해당 release의 워커만 기동한다. 원본 fixture 재생성은 실행하지 않는다.
3. 해당 release manifest에 지정된 guard를 기동한다. 키 저장소 오류나 포트 충돌은
   자동 회피하지 않고 중단한다.

```sh
"$PY" "$RELEASE/experiments/m4/a3/run_stack.py" --root "$ROOT" up \
  --model opencode-go/muse-spark-1.3-contributor --model-probe-status unverified
SPEC="$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["guard_spec"])' "$ROOT/run/active-release.json")"
# 사용자가 직접 여는 관리 터미널에서 실행. 필요 시 승인된 관리 실행 방식으로 분리한다.
"$PY" "$RELEASE/experiments/m4/a3/tunnel_guard.py" --spec "$SPEC"
```

정상 중지는 **진행 중 작업이 없는지 확인 → 실제 guard PID와 명령·소유자 대조 →
guard에 SIGTERM → endpoint·wrapper PID와 명령 대조 → 워커 종료** 순서다.
`run_stack down`은 PID 파일을 읽으므로, 소유권을 검증하지 않은 채 단독으로 실행하지 않는다.
과거 PID를 복사하거나 `pkill opencode`, 전체 tunnel-client 종료를 사용하지 않는다.
종료 확인이 불확실하면 새 인스턴스를 띄우지 않는다. 원장·결과·기존 로그는 삭제하지 않는다.

가드는 자기가 실행한 터널 프로세스 그룹만 관리한다. 정상 중지는 재시작하지 않고,
비정상 종료는 5초부터 최대 60초 백오프 및 연속 실패 예산을 적용한다. 터널 복구는
모델 요청 재전송이 아니며, 이미 전달된 job을 자동 재실행하지 않는다.
가드 자체 종료·OS 재부팅·로그아웃까지 생존한다고 보장하지 않는다.

## 결과·재시도·승인

- 연결 실패와 작업 실패를 구분한다. 시간 초과 뒤 새 request_id를 만들어 다시 시작하지 않는다.
  먼저 원장/동일 request_id의 접수 여부와 기존 job을 확인한다.
- `waiting_approval`에서 challenge나 fixture CODE는 사용자 승인 증거가 아니다.
  승인되지 않은 동작을 자동 허용하지 않는다.
- `acceptance: pending`은 작업 결과의 수용 필드다. 프로젝트 출시 판정과 다르며 DB를 직접 수정하지 않는다.
- `worker_wait.wait_expired`는 호출 예산 만료이지 job 실패가 아니다. 현재 구현은 호출 시작
  스냅샷과의 변화를 기다리므로 이미 완료된 job에도 `completed + wait_expired:true`가 가능하다.
  최종 판정은 `job.state`, 검증 상태 및 커밋된 `worker_result`로 확인한다.
- `workspace:null`은 inspect에서 작업용 worktree가 생성되지 않은 경우 허용된다.
  요청 저장소는 job의 `repository_path`/`base_revision`으로 감사한다.
- billing은 분류 근거가 없으면 `unknown`; probe 상태는 startup만으로 `live_verified`로 올리지 않는다.

## 기록과 보안

`logs/a3-tunnel-client.log`, `logs/a3-tunnel-guard.log`, `logs/endpoint.err.log`,
`logs/supervisor.out.log` 및 `db/worker.db`를 구분한다. 임의의 원시 payload·인증값을
Git에 넣지 않는다. 터널 로그는 기존 파일도 소유자/regular file 여부를 검증해 0600으로
낮추며 symlink/hardlink/FIFO를 거부한다. 보호된 런타임 루트의 부모 디렉터리를 임의 교체하지 않는다.

DB 백업은 SQLite backup API로 일관된 스냅샷을 만든다. 결과 감사는 이전 레코드 보존과
신규 허용 레코드 추가를 분리한다. 새로운 job이 정상 추가된 뒤 전체 DB가 이전 해시와
동일해야 한다고 검사하지 않는다. provider 측 독립 dispatch 이벤트가 없는 범위는 명시한다.

## 원복

먼저 guard와 워커를 소유권 확인 후 정상 중지한다. 배치 기록의 이전 프로필/코드 경로를
복원한 뒤 동일 모델로 수동 기동한다. DB는 기본적으로 **그대로 보존**하며 이전 백업으로
자동 덮어쓰지 않는다. 백업 뒤 새 job이 있을 수 있다. transport만 원복하는 경우 supervisor와
OpenCode는 건드리지 않는다. 이전 코드로 돌아가는 것이 장애 해소를 보장하지는 않는다.

## 남은 범위

영구 LaunchAgent는 별도 승인 사항이다. 설치 요청 시 endpoint/supervisor/guard의 기동
순서, 중복 방지, 종료, 키 접근, 재부팅 시험 및 원복을 포함한 한정 승인안을 제출한다.
M0의 OPEN6 및 API session과 raw-run session 식별 차이, ResourceWarning은 기존 제한사항으로
보존한다. M3 정책 모듈의 통합은 완료해도 전체 런타임 배선까지 검증한 것으로 표현하지 않는다.
