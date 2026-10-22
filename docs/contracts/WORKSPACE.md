# 작업공간/Worktree 구현 절차 v0.3

## 1. 준비와 입력 보존

start.workspace.repository는 기존 실제 저장소 root의 absolute path. `realpath`, `git rev-parse --show-toplevel`, `--git-common-dir`로 확인한다. 공백/한글은 허용하고 subprocess argv로 전달한다. 경로 prefix만으로 Git root를 추정하지 않는다. source_ref는 `git rev-parse --verify <ref>^{commit}`로 commit SHA에 고정, 옵션 주입 방지를 위해 ref validation과 `--end-of-options` 지원을 시험한다.

원본에서 `git status --porcelain=v1 -z`, staged/unstaged diff, untracked 경로 및 필요한 파일 metadata/hash를 **읽기만** 한다. 원본 index/config 변경·stash/reset·암묵적 commit 금지.

기본 include_existing_changes=false. 원본 dirty인데 사용자가 clean committed 상태만 대상으로 한다는 명확한 지시(`use_committed_only=true`)가 없으면 WORKSPACE_DIRTY로 보고하여 누락 입력을 조용히 버리지 않는다. include_existing_changes=true여도 workspace.context_files의 명시 목록만 가져온다. baseline manifest를 읽기 전/후 비교하여 원본이 변했으면 WORKSPACE_CHANGED. token/.env/인증파일을 자동 복사하지 않는다. 작업에 필요한 자료의 종류 자체를 차단하는 규칙은 아니다.

## 2. 생성/lease/PWD

job/attempt intent를 durable 기록한 뒤 `.../worktrees/<job_id>/<executor_id>`에 detached Worktree를 생성한다.
`git worktree add --detach <new_path> <base_sha>` (모든 인수 별개).
기존 dir는 덮어쓰지 않는다. canonical 경로·writer lease·Git common-dir 제어 lock 기록. Worktree 생성 실패는 해당 작업만 failed, 원본은 그대로 유지.

실행 cwd=PWD=canonical worktree. backend session location도 동일한지 확인하고, 첫 쓰기 전에 read/pwd probe로 검증한다. mismatch는 CONTRACT_MISMATCH, 쓰기 실행 금지. `PWD`만 바꾸고 실제 cwd를 놔두거나 반대로 하지 않는다.

## 3. dirty 입력·새 파일 적용

기존 staged와 unstaged 내용을 합친 원본 working state를 선택 파일 manifest로 캡처한다. 전용 Worktree에서 `git apply --check` 후 patch 적용, untracked 새 파일은 allowlisted relative path+raw bytes/sha로 별도 복사. symlink는 외부 target을 dereference하여 복사하지 않고 link 자체를 기록한다. unsafe path(`..`, absolute, .git metadata), case-insensitive 충돌·NUL은 거절.

처리 전 선택된 input snapshot hash를 확정하고 worker에 전달한다. 원본 dirty manifest와 차이가 생기면 중단한다. 바이너리는 bytes archive+hash로 취급, submodule/gitlink는 변경 감지·보고만 하고 첫 버전 자동 초기화/수정 금지(CAPABILITY_UNVERIFIED). exclusions는 캐시 여부+입력 정책에 명시하고 정상 소스를 조용히 제외하지 않는다.

## 4. 결과 수집 — 신규 파일 누락 방지

writer 종료/정지 뒤 baseline 대비 추적 파일 변경·삭제와 untracked 신규 파일을 각각 수집한다. `git diff <base>` 하나로 끝내지 않는다.

- 추적 파일 patch는 `git diff --no-ext-diff --no-textconv --binary <base_sha>` 또는 검수된 동등 경로. 적용 가능한 전체 patch를 보존.
- 신규 파일은 `git ls-files --others --exclude-standard -z`와 작업중 생성 manifest 대조. ignore된 요청 산출물이 누락되면 명시적으로 보고/수집.
- changes manifest: relative_path, change_kind, file_type, before_sha256/after_sha256(nullable), size, mode, artifact_id. rename은 old/new 모두 기록. binary/link/삭제는 빈 텍스트로 대체하지 않음.
- source snapshot hash는 정렬된 `{path,type,mode,size,content_sha256}` canonical JSON의 SHA256. 파일 내용 해시는 가공 전 실제 bytes. exported patch의 hash와 구별.
- 출력 잘림은 preview에만 허용. 전체 private 산출물을 먼저 보존하고 ChatGPT에는 페이지 export. 32k/500k 혼합 상한으로 잘린 patch를 complete로 표시하지 않음.

## 5. 독립 검수자의 정확한 대상

코드 작성자는 의도된 파일·새 파일·테스트를 자기 branch에 commit하여 source SHA를 제출하는 것이 개발팀 기본 경로다(해당 티켓의 commit 허용 범위 안). runtime worker는 별도 지시 없이 commit/push하지 않는다.

```sh
# coordinator/reviewer의 개발 검수. WORKER_SHA는 인수인계에 적힌 실제 SHA.
git -C "$ROOT" fetch origin
# worker 브랜치가 원격이면 정확한 SHA를 재확인한다.
git -C "$ROOT" worktree add --detach "$REVIEW_WT" "$WORKER_SHA"
git -C "$REVIEW_WT" rev-parse HEAD
git -C "$REVIEW_WT" status --porcelain=v1
```

미커밋 runtime 결과를 검수할 때는 reviewer 전용 Worktree를 base_sha에서 만들고 **전체 raw patch+신규파일 manifest**를 재현한다. 재현 후 source snapshot hash가 writer 제출 해시와 일치해야 테스트한다. revision이 다르거나 신규 파일 누락이면 검수 BLOCKED. 원본 repo HEAD에서 테스트한 성공은 worker 결과 검수로 인정하지 않는다.

reviewer가 고칠 필요를 발견하면 작성자에게 돌려주거나 별도 수정 ticket/worktree로 넘긴다. 검수 중 수정한 뒤 동일 diff를 자기 승인하지 않는다. source SHA가 바뀌면 새 review 필요.

## 6. 원본 반영·통합·정리

사용자 또는 담당 coordinator의 해당 변경 통합 지시가 있을 때만 진행. 원본 현재 HEAD와 dirty 상태를 인수 시작 시점과 비교하고, 바뀌었으면 자동 덮어쓰기 대신 재base/충돌 검토. immutable result snapshot의 검수 결과가 적용 대상과 일치해야 한다. 일괄 `git add .`, force push, `reset --hard`, 원본 자동 stash 금지.

commit된 개발 결과는 검수 후 PR/fast-forward/cherry-pick 등 명시 선택으로 통합. runtime patch 결과는 `--check`와 base 검증 후 별도 통합 Worktree에서 적용·검사한다. 실제 원본 파일 반영은 별도 지시 범위다.

## 7. Git 없는 자료와 운영 작업

inspect는 명시된 원본 파일/로그 읽기에 사용하며 테스트를 원본에서 실행하는 우회 경로가 아니다. 비-Git 수정은 선택 자료만 별도 seed repository에 복사하고 그 seed의 worker/reviewer Worktree에서 변경안을 만든다. 첫 M1은 비-Git 수정 capability=false, M2가 구현. 자료 종류 때문이 아니라 작업공간 기능 미구현 때문임을 표시한다.

실제 서비스/시스템 설정은 Worktree로 격리할 수 없으므로 operate 별도 프로필/승인/복구 계획이 필요하다. Worktree는 OS 보안 샌드박스가 아니며 공유 Git 메타데이터·네트워크 부작용을 분리하지 못한다. M0/M1에서는 사용자 서비스나 DB를 시험 대상으로 사용하지 않는다.

## 8. 기계 입력 보완

`include_existing_changes=true`와 `use_committed_only=true`를 동시에 허용하지 않는다. 첫 번째는 상대경로 context_files 목록이 필수, 두 번째는 원본 dirty 변경을 포함하지 않는다는 명시 선택이다. 둘 다 false이면 dirty 원본을 거절한다. context_files는 repo root 기준 실제 파일의 상대 경로이고 glob/디렉터리 재귀 선택은 초기 미지원이다. 존재하지 않거나 root 밖으로 canonical resolve되면 입력을 거절한다. source_ref의 옵션처럼 보이는 값은 실행 전 거절한다.
