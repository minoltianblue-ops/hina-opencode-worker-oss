> Historical operator reference, not a portable one-command installer. Verify privileges, process ownership and environment before live operations.

# M4-A2 — supervisor 관리 실행(runbook)

수동 기동 전용 상주 실행. 대상 구조(PLAN §4): supervisor가 실행을 소유하고,
터미널 뷰어는 관측만 한다. 이 문서의 모든 경로는 이 저장소 루트 기준이며
**Mac mini에서 수동으로** 실행한다. launchd 등록은 이 범위가 아니다(§5).

## 1. 수동 기동 / 중지 / 상태

```sh
# 기동(foreground 관리 프로세스; 터미널 종료/SIGTERM이 정상 중지 신호)
python3 experiments/m4/a2/supervise_run.py run --root <ROOT>
#   <ROOT>: supervisor 전용 디렉터리(0700). run/db/keys/artifacts/pool/logs를 생성·사용.

# 상태 한 번 (READY exit 0 / NOT_READY exit 1)
python3 experiments/m4/a2/supervise_run.py status --root <ROOT>

# 관측 전용 터미널 뷰어 (Ctrl-C로 빠져나가도 작업 무영향 — T15)
python3 experiments/m4/a2/supervise_run.py viewer --root <ROOT>            # 스냅샷 1회
python3 experiments/m4/a2/supervise_run.py viewer --root <ROOT> --watch --interval 2

# 정상 중지: 래퍼에 SIGTERM — 자식 supervisor를 close()로 종료시키고 재기동하지 않는다
kill -TERM <wrapper pid>   # run-events.log 첫 줄에 기록됨
```

백엔드: 이 수동 실행은 `supervisor_proc.py --backend fake`(스크립트형,
실제 프롬프트 0건·모델 호출 0건, D2). `--backend live`는 이 패키지에서
**거부(exit 2)** — 라이브 서비스 엔트리와 라이브 모델 호출은 각각 별도
사용자 승인 후 확정된다.

## 2. 크래시 재기동과 저널 보존

- 자식 supervisor 예기치 종료(크래시/시그널) → 래퍼가 백오프(기본 0.5s,
  연속 크래시 시 2배씩 최대 10s)로 재기동. `run-events.log`에
  `child_exit`/`restart`가 남는다.
- 저널은 `<ROOT>/db`(SQLite, WAL, synchronous=FULL)에 있어 재기동으로
  손상되지 않는다. 재기동 시 `reconcile_after_restart`(STORAGE.md §3 ④)가
  미확정 dispatch를 `needs_attention`+`ambiguous`로 **추가 기록**할 뿐
  기존 state_events/requests 행을 수정·삭제하지 않는다. 크래시 중
  running이던 작업은 needs_attention으로 관측되며 자동 재디스패치 금지.
- 죽은 supervisor가 남긴 `run/supervisor.sock`은 래퍼가 **flock 검증 후**
  (살아 있는 리스너 없음 확인 시에만) 제거한다. supervisor 자신은
  알 수 없는 소켓을 unlink하지 않는다(M1 설계) — 정리 책임은 관리 실행.
- 크래시 폭주: 연속 `--max-fast`(8)번 `--min-up-s`(2s) 미만 종료 시
  래퍼가 exit 70으로 포기 → 바깥(예: launchd ThrottleInterval)이 절제.
- 래퍼를 SIGKILL하면 자식이 고아로 남을 수 있다(정지 신호는 SIGTERM).
  고아 처리는 운영자 또는 바깥 감독자 책임 — plist 초안의 stop 경로는
  SIGTERM이다.

## 3. 로그 보존

`<ROOT>/logs/`의 두 스트림(`supervisor.out.log`, `run-events.log`)은
크기 기반 회전: 기본 최대 256KiB × 총 4파일(`--log-max-bytes`,
`--log-keep`). 기록 전 크기 검사라 파일당 최대 한 줄만큼 초과 가능.
암시적 자동 삭제는 없다(PLAN §2: 로그 자동 삭제 미추가) — 상한이 보존
정책이다. 사용자 데이터는 기록되지 않는다( stdout/stderr + 운영 이벤트만).

## 4. 터미널 뷰어 배선(관측 전용)

`viewer` 서브커맨드는 `hina_opencode_worker.viewer --socket <ROOT>/run/supervisor.sock`을
exec한다(프로세스 추가 없음). 뷰어 클라이언트 역할은 서버측에서
snapshot/tail/requests로 제한 — 그 외 메서드는 FORBIDDEN(증거
`viewer-mutation-refused.json`). 뷰어 종료·터널 단절은 소켓 하나만
해제하며 작업에 영향을 주지 않는다.

## 5. launchd — 초안만 (설치 = 별도 승인)

초안: `docs/deploy/launchd/com.hina.opencode.worker.supervisor.plist.draft`
(User-level LaunchAgent, KeepAlive는 래퍼 위에 얹는 이중 감독; `__PLACEHOLDER__`
필요 시 채움). **설치 금지**: `launchctl bootstrap` 등 영구 서비스 등록은
M4 범위에서 별도 사용자 승인 사항이며, 라이브 엔트리·바이너리 핀 확정과
동시에 처리된다. 현재 상태 검증은 `plutil -lint`까지
(증거 `plist-lint.txt`).

## 6. 검증/잔여 점검

- 단위: `PYTHONPATH=src:tests python3 -m unittest tests.test_a2_supervise_run` (T-A2-1..4)
- 실증: `python3 experiments/m4/a2/run_evidence.py --out <temp>` 후
  `docs/evidence/m4/a2/<run_id>/SUMMARY.md`·`manifest.json` 대조
  (수동 기동→SIGKILL 크래시→자동 재기동→저널 동일→뷰어 관측→정상 중지).
- 잔여 0: `pgrep -fl supervise_run.py; pgrep -fl supervisor_proc.py` 출력 없음.
