# Order lane 기억

규칙은 `docs/rules.md`가 우선한다. 번호는 근거 이슈다. 이름은 2026-11-02 main 기준으로 확인했다.

## 재시도·복구: 다시 만들기 전에 확인

- 크래시·정지·시간 초과·새 게이트 재시작 뒤 작업은 브랜치에 WIP·병합 커밋으로 남는다. `git log --first-parent`, `git merge-base --is-ancestor origin/main HEAD`, `git diff --stat origin/main...HEAD`로 확인하고 다시 구현하지 않는다. 사라지는 것은 `.foragent/`(TRY·SEARCHED·REBUTTAL)와 `.venv`뿐이니 Done when을 돌린 뒤 다시 쓴다. TRY의 통과 개수는 실제로 돌린 값으로 쓴다.
- 새 작업 트리에는 `.foragent/`가 없다. 변이 백업 전에 `mkdir -p .foragent`를 하고 백업 명령을 `|| exit 1`로 묶는다(변이가 원본에 겹쳐 남은 적 있음). `$(cat .foragent/x)`가 비면 `fa test`가 전체 묶음을 돈다.
- 전달된 실패 사유를 현재 사실로 믿지 않는다. 충돌 목록은 status·MERGE_HEAD·실제 표시로, 시간 초과는 로그 끝으로 확인한다. "Too big for one session"의 실제 원인이 병합 차례 대기 예산일 수 있다(#1006). 기준선 시간 초과 로그는 실패 표시만 있고 상세가 없다(#1206). 그때는 제품 코드나 게이트 한도를 바꾸지 않고 해당 노드만 재검증해 구분해 기록한다.
- 리뷰 PASS 뒤 재시도는 새 설계가 아니라 운영 사유일 수 있다(새 게이트 이동, base-changed). 검토·CI 통과 힌트가 있으면 인계 기록만 복구한다.
- 같은 결함 종류로 두 번 돌아오면 땜질 대신 재설계한다(D106). 근거: #1043은 한 이슈에 묶음 발행·큐·저널·복구를 넣어 리뷰 24회 뒤에도 끝나지 않았고, 쪼갠 #1262·#1263·#1264·#1312는 리뷰 1~3회로 머지됐다. #1107은 9차까지 재승인·기록을 쌓다가 거절 경로만 남기는 재설계로 끝났다. #1047은 맥락 복사를 항목별에서 폴더째로, #1178은 내용 비교를 사건 id로 바꿔 끝났다.
- 재시도 힌트는 마지막 지적 줄만이 아니라 그 결함 종류 전체로 본다. 지적된 줄 밖의 같은 종류를 찾고 호출자 전부를 확인한다.

## main 동기화·병합

- main에 새로 생긴 인수 파일이 diff에서 "삭제"로 보여 리뷰가 거절하면 그 파일만 `git restore --source=origin/main --worktree -- <파일>` 뒤 `git add -N`으로 표시하고 `git hash-object`로 같은지 본다. 먼저 `git fetch origin && git diff --name-only HEAD...origin/main`을 본다. 소스 파일은 가져오지 않는다. 러너가 전부 stage해 리뷰하므로 "범위 밖 변경"으로 거절된다(#1025, #1047).
- 병합 중(MERGE_HEAD 있음)에는 새 rebase를 시작하지 않는다. `git diff --name-only MERGE_HEAD origin/main`만 가져온다. `git merge`·`git checkout --theirs`·`git commit`은 권한이 막힌다. 겹친 파일은 `git merge-file`, main 판을 받으려면 `git show MERGE_HEAD:<파일> > <파일> && git add`. 러너가 해결본을 stage하니 커밋하지 않는다. index.lock 쓰기가 거절되면 파일 해결과 인덱스 UU를 구분해 SEARCHED에 적는다.
- `tests/test_order.py` 끝에 양쪽이 시험 묶음을 더한 충돌은 둘 다 보존하고, HEAD·MERGE_HEAD(`git show :2/:3`) 최상위 AST 이름의 합집합이 결과와 같고 중복이 없는지 본다(`grep -oE "^def [a-z_]+" | sort | uniq -d`).
- 자동 병합 부분도 본다. 충돌 밖에 옛 서명이 남을 수 있다(#1191 `_publication_checks`). 공용 함수가 옮겨진 충돌은 HEAD 블록을 버리고 호출을 main의 새 자리로 옮긴다.
- 내 수정과 main 수정이 같은 결함을 고치면 합치지 않고 범위 밖인 내 쪽을 main 판으로 되돌린다(#1189 test_presence, #1049 경쟁 수정). 그러면 내 수정에서 나온 리뷰 지적도 함께 사라진다.
- 병합 뒤 `PublishReport`·`_Publication` 같은 공용 구조에 필드가 같은 자리로 들어오면 둘 다 보존하고 생성자를 전부 검색한다(#1107 affected + #1263 waiting_for). `order_waiting.section`과 `owner_error`처럼 맥락 키도 함께 남기고 `docs/impl/p8a-order-tools.md`를 합친다.
- `src/foragent/order.py`는 이 레인의 충돌 지점이다(#1158은 제자리 해결 2회 실패로 retries-exceeded). 작은 도우미 모듈에 넣고 order.py에는 호출 한 줄만 둔다.

## CI·기준선 판정

- "CI failed" 내용이 비면 `gh pr list --head <브랜치> --json statusCheckRollup` → `gh run view <id> --job <job> --log-failed`. 실행 중이면 `gh api --allow-escape-sequences repos/<r>/actions/jobs/<job>/logs`로 끝난 칸 로그를 읽는다. `collect` 칸은 수집 오류일 수 있으니 `^ERROR`와 `tests collected`를 본다.
- 내 탓인지 먼저 가른다: 같은 시험이 main CI(`gh run list --branch main --workflow ci`)에서도 실패하는지, `git diff origin/main...HEAD -- <시험·대상 코드>`가 비는지. 같으면 고치지 않고 근거를 REBUTTAL·SEARCHED에 적는다. 같은 실패가 두 번이면 레인 밖 main 결함이니 오케스트레이터에 넘긴다. 남의 산출물을 대신 만들지 않는다(빈 도우미는 잠금 뜻을 지운다, #1191 merge_path_expected_red).
- 시각에 따라 갈리는 시험을 의심한다. 고정 시계와 실제 시계가 섞이면 특정 UTC 시간대에만 실패한다(#1252 inspect notify, #1237 presence, 둘 다 main에서 고침).
- 기준 비교: 바꾼 파일만 main 판으로 바꾸고 FAILED·ERROR를 정렬해 `.foragent/{mine,base}.txt`로 diff한 뒤 finally에서 복원하고 sha256으로 확인한다. stash는 `fa test`가 죽으면 남으니 쓰지 않는다. 내 새 모듈은 지우지 않는다(수집 오류가 된다). 파일 하나씩 또는 작은 묶음으로 돌린다.
- 이 저장소는 main에서도 실패하는 인수 시험이 늘 있다. 고정된 실패 목록을 믿지 말고 그때 기준을 잰다.
- `.foragent/acceptance-exclusions.json`은 러너가 기준 커밋 실패 시험으로 만들어 강제 추가한다. "내 시험 제외"로 지적되면 REBUTTAL로 답한다.
- 이 기계의 셸은 `PYTHONDONTWRITEBYTECODE=1`이라 CI와 다르다. 사본에서 import가 일어나는 시험은 `env -u PYTHONDONTWRITEBYTECODE fa test ...`로도 돌리고, 시험 저장소 기준 커밋에 `.gitignore`(`__pycache__/`)를 넣는다(#1049).
- 단독 통과로 CI 실패를 닫지 않는다. 원본 로그를 본다. `from x import y`로 묶은 공장은 첫 import 순서에 따라 가짜가 영구 보관된다(#1049 `executors.executor_for`, #414, #1003 `capacity.machine_root`). 시험이 바꿔 끼우는 이름은 호출 때 모듈 속성으로 읽는다.
- 보류 사유가 코드 결함이 아닐 때도 있다. "Unusable acceptance shard"(#1046, #1054)와 리뷰어 답이 `(none)`인 보류(#1191)는 코드를 고칠 근거가 아니다. 사유 원문을 확인하고 오케스트레이터에 넘긴다.

## 검증 방법

- 관련 시험만 돌린다. `fa test tests/` 전체는 10분 제한을 넘고, 부하가 높으면 잠긴 시험 43개도 7분 넘게 걸리니 묶음을 나눈다. 파일 목록이 비면 전체가 돈다.
- 샌드박스에서 `fa test`가 공용 `test-slots.lock` 쓰기 거절로 멈추면: 기존 guard/slot 파일을 읽기 descriptor로 열어 `capacity.hold_test_slot`의 슬롯 수·메모리·`capacity.SLOT_POLL_SECONDS` 그대로 flock을 잡고, 잡은 부모만 자식 `fa test`에 `FORAGENT_TEST_SLOT=held`를 넘긴다. `UV_CACHE_DIR=$PWD/.foragent/uv-cache UV_NO_SYNC=1 UV_OFFLINE=true`와 기존 `.venv`를 쓴다. 남는 슬롯 파일로 입장을 우회하지 않는다(설정값과 effective_test_slots가 다를 수 있음). `uv run --no-sync`는 빈 `.venv`를 만들어 쓰지 않는다. 못 돌렸으면 "미확인"으로 적는다.
- 변이 검사는 `.foragent/*.bak` 복사 → 변이 → 실패 확인 → 복원 → `git hash-object` 비교. `git checkout`은 막힌다. NameError 실패는 증거가 아니니 대상 모듈의 import를 먼저 본다. 제품 파일을 건드리기 싫으면 `.foragent/`의 임시 pytest 플러그인(`-p`, `PYTHONPATH=.foragent`)으로 함수를 바꾼다.
- 변이가 살아남으면 세 가지를 본다: 다른 결속이 이미 같은 것을 묶는지, 시험 데이터가 그 조건만 가르는지(SHA 고정 고정물은 반복을 가린다), 그 코드가 필요 없는지. 겹치는 보호는 함께 꺼야 옛 동작이 재현된다. 첫 거절을 지워도 잠금 안 재검사가 가리니 부수 쓰기가 없었는지까지 단언한다.
- 거절을 더한 검사는 고정물을 "잘못된 이유로 통과"시킬 수 있다(`code != 0 and 경로 in 출력`). 검사 함수를 감싸 시험별 거절을 기록하는 임시 플러그인으로 발행 시험 전체를 돈다(#1206).
- 시험 만들기: 다른 clone 경쟁은 실제 git clone + 양쪽 순서 × 모든 경로로. 다른 기계 push·로컬 커밋은 `git fast-import`(하네스가 `git commit`을 막음, 로컬 커밋은 fast-import + `read-tree -m -u old HEAD`, 추적 링크는 `M 120000`, 비선형 교체는 `--force`). 실패 주입은 `monkeypatch.setattr(tracker, "comment"/"update_issue"/"add_blocked_by", ...)`(update_issue는 키워드 인자). `offline_publication`의 push는 bare origin ref만 옮기니 원격 효과는 origin의 `refs/heads/main`을 보거나 `git.update_base("main")`을 부른다. Unix 소켓은 `monkeypatch.chdir` 뒤 짧은 이름으로 bind. `editor.write(x) or editor.flush()`는 flush를 안 부른다.
- 시험 시계: FakeTracker 직접 발행은 로컬 목록을 갱신하지 않으니 끼워 넣은 틱 뒤 시계를 10분 넘긴다. 프로세스 표는 conftest 가짜이니 살아 있는 주인은 `processes.list_processes`를 monkeypatch한다. capsys는 stderr도 잡으니 디버그 출력은 `.foragent/` 파일에. HOME/XDG는 conftest가 모두 격리한다(#655). 테스트 격리 HOME이 저장소 아래 있으면 `StateStore` 쓰기가 "공유 변경"으로 잡히니 잠긴 고정물은 HOME을 제외한다(#1262·#1263에서 시험 소유자가 고침).
- `tests/conftest.py`를 `## Files` 밖에서 바꾸면 harness guard가 사람에게 보류시킨다(#1523 PR 1562). 새 관문 때문에 고정물이 깨질 때 conftest에서 검사를 끄는 스위치를 넣지 않는다. Files에 선언하거나 QUESTION으로 넘긴다.
- 셸: zsh는 변수를 안 쪼갠다(`${=v}`, `files=(${(f)"$(...)"})`). `--include=*.py`는 따옴표로, `echo ==`는 오류, `:$s:t...`는 수식어로 먹으니 `${s}`. 파일 인자 없는 `cat >`는 멈춘다. macOS에는 `timeout`이 없다. /tmp·$TMPDIR 대신 저장소 안 `.foragent/`를 쓴다. 제한 환경 uv 캐시는 `UV_CACHE_DIR`로 지정한다.

## 잠긴 시험과의 충돌

- 잠긴 시험끼리 또는 시험과 스펙이 정면 충돌하면(#1108, #1262, #1263, #1312, #1556) 제품을 시험에 맞춰 우회하지 않는다. 재현 근거와 추천을 `.foragent/QUESTION.md`에 남기면 시험 소유자가 main에서 고친다. 구현은 작업 트리에 남긴다. 고쳐진 뒤 재개는 구현을 다시 쓰지 않고 검증만 한다.
- 새 거절을 더해 잠긴 시험의 옛 기대(예: Done when 줄을 설명 없이 잃는 수정)가 깨지면 Done when이 통과해도 회귀로 본다(#1556, stop hook이 잡음). 파서·관문을 바꾸면 `grep -rlE "unknowns|preread\(" tests`처럼 의존 시험을 찾아 모두 돌린다.
- 줄 수 한도: `test_scheduler_split.py`·`test_cli_split.py`의 `BASE_MODULES` 밖 모듈과 `commands/*.py`는 300줄이다. `order_prepare.py`(297), `order_prepare_cycle.py`(286)는 거의 찼다. 힌트가 "네 탓 아님"이어도 실패 메시지의 모듈 이름을 먼저 본다.
- 문서 시험은 문단(빈 줄)·글자 거리·첫 등장 위치로 본다. 제목의 낱말도 세고(#773), 백틱을 빼고 세고, `--approve`는 `(?<!-)`로 거른다(#1192). `SKILL.md` 한도는 splitlines 기준이다(fa-orchestrator 150줄). `test_fa_order_talk.py`가 "session settles reversible details itself ... in the records" 문장을 잠갔으니 지우지 말고 덧붙인다. `test_ask_through_planning`이 일반 질문 규칙을 잠갔다. fa-order 문구를 바꾸면 `grep -rlE 'fa-order|fa-orchestrator|order-card' tests/acceptance`를 돌린다.
- init에 질문을 더하면 `input`을 막거나 수를 세는 잠긴 시험이 깨진다: `grep -l builtins.input tests -r | xargs grep -l '"init"'`, `test_statusline_wrapper.py`, `test_cli_split.py`(stdout 정확 비교), `tests/test_cli.py`(`cli.load_config` 호출 기록).
- README·발표 초안·gate.md를 고치면 `test_launch_*`, `test_docs_role.py`, `test_voice.py`를 돌린다. 한국어 메시지는 해요체(`니다` 금지). 수치·한계는 README에서만 가져온다. 구현 주체 문구는 init 기본값을 따른다.
- 동작 확인만 하는 이슈(#1106)는 제품 코드를 바꾸지 않는다. Done when을 돌리고 변이로 시험이 잡는지 확인한다.

## 리뷰가 잡은 결함 종류

- 사실보다 먼저 기록: 상태 댓글은 그 사실이 끝난 뒤에 남긴다. 승인을 내리는 쪽만 먼저 한다(안전한 쪽). edit 순서: 승인 해제 → 본문 → 간선 → 대기 줄 → revision 댓글(#1178).
- "이미 말했다"를 기록끼리 내용으로 비교: A→B→A→B를 못 가린다. 변화마다 새 id를 본문과 함께 쓴다. 최신이 아닌 `any(...)`는 과거 발행을 지금 승인으로 착각한다(#1178).
- 사본·파생 기록을 원본처럼 믿음: 연결 사실은 쓰기와 함께 남는 원본(자식 본문 표식)에서 읽는다(#1049). 발행 여부는 이 clone의 미발행 이력이 아니라 원격 이력의 사실이다(#1312).
- 확인 뒤 낡은 검사(TOCTOU): 검사는 발행이 실제로 쓸 바이트로 한다. 검사와 쓰기를 같은 잠금에 넣는다. "또 한 번 검사"는 해결이 아니다. 긴 작업 뒤 base를 다시 읽는다. 첫 검사 전 fetch하고 잠금 안 재검사보다 앞에 쓰기(`ensure_labels`)를 두지 않는다. 시간 창이 짧다는 반박은 받아들여지지 않는다.
- 모르는 것을 나쁜 쪽으로 판정: 생존 불명은 죽음이 아니다(`alive(owner) is False`일 때만 넘겨받기). 읽기 실패로 소유 증거를 지우지 않는다. 정리는 관찰된 사실에만 한다(#1003, #1050).
- 프롬프트로만 막은 권한: 구조로 막는다(샌드박스, 사용자 설정·hook·MCP 끊기). 스크립트가 후보 시험을 호스트 권한으로 직접 돌리면 결함이다(#1047, #1048).
- 경로·임시 파일 소유: 쓰기 전 거절은 목적지 충돌까지 본다. 심볼릭 링크를 따라가지 않는다. 고정 이름 임시 파일을 지우지 않는다(O_EXCL로 자기 것만). `update_base` 뒤 다시 검사한다(#981, #1043, #1189).
- 복구 경로 누락: 새 공용 검사를 새 경로에만 합치면 복구 경로가 빠진다. 체크포인트 사이의 모든 중단을 본다. 거절을 더할 때 메시지가 안내하는 복구 길이 모든 대상에 닿는지 본다(#1191 statement 이슈 다시 읽기, #1264, #1312).
- 맥락 손실: 사실을 boolean으로 줄이지 않는다. 주기가 묶는 맥락은 에이전트도 모두 받는다(#848, #1048).
- 무시되는 사용자 언어: 사람이 보는 모든 글(오류·argparse help·질문)은 `messages/` 등록부와 `config.language`로 낸다. 에이전트 프롬프트는 영어로 둔다. 공유 모듈에 영어 오류를 더하지 않는다. `{key}`는 `text(key, language, **values)`와 충돌한다. 파서 생성 중 `load_config`를 부르지 않는다(#414, #419).
- 리뷰 반박: hunk만 본 추정("예외가 잡힌다", "키가 없을 수 있다", edits 필드 누락)은 전체 제어 흐름·실제 계약·호출부를 직접 확인하고 시험을 더해 REBUTTAL로 답한다. 방어 코드를 늘리지 않는다(#1049, #1107 `blob_shas`의 `"missing"`, #1191, #1206 validate_plan required). 다른 이슈의 파일 지적은 `git diff origin/main -- <파일>`이 비는지로 답한다.

## 발행 구조 (`order.py`, `order_flush.py`)

- 공용 검사는 `order._publication_checks`(→`_Publication`: by_number·own·files, 일반 발행·flush 공용), 쓰기는 `_publish_issues`, 대상 탐색은 `_publication_targets`(preread와 공유). 새 관문은 validate_plan 직후 또는 이 함수에 둔다. 일반 경로만 고치면 flush가 빠진다. dry-run의 조기 복귀가 어떤 검사보다 앞인지 본다. 진단 여러 줄을 담는 전용 오류는 flush의 catch에서 일반 PlanError로 바꾸지 말고 그대로 전달한다(#1206).
- Git 단계는 `git.publish_lock()`(git common dir) 안에서 `git.update_base(branch)`(fetch 후 ff-only)부터. 덕 타입 가짜 `tests/test_order.py::PublishingGit`에 메서드를 함께 더한다. `is_ancestor`는 origin/main이 HEAD 조상임을 나타내야 한다. publish_lock은 다른 clone을 못 막는다. clone 사이는 공유 상태의 `prepare/route.lock`(`order_prepare_owner.publication_gate`/`claim`)을 쓴다.
- 파일 쓰기: `order_files.check_destinations`(validate_plan 끝) + `order_files.check_write`(쓰기 직전). `validate_plan`은 `str | bytes`를 돌려준다. 새 쓰기 경로도 같은 검사를 거친다.
- 잠긴 시험 보호(#1107): `order._affected_owners`. 주문 밖 열린 approved 이슈의 잠금은 본문 Done when의 인수 경로(`issue.acceptance_paths`, 인수 폴더·하위 폴더 포함, 게이트와 같은 근거). `own`은 이번 계획에 남은 자식+edits. 겹침은 `_under_lock`(`lock + "/"` 경계). 후보는 계획 파일 + `git.committed_paths(origin/base)`(push가 옮기는 로컬 커밋). dry-run은 목록, 실제 발행은 쓰기 전과 `update_base` 뒤 거절, flush는 base 갱신·전체 preflight 뒤 selected 저널 전에 호출. 게이트·승인 기록·재승인은 건드리지 않는다.
- 게이트 승인 제약: `scheduler_approval._snapshot`은 편집 시각 >= 승인 시각이면 승인을 무효로 하니 만들고 나서 본문을 고치면 같은 초의 `--approve`가 풀린다. `test_tick_github_budget.py`, `test_scheduler.py`의 틱당 fetch 1회가 게이트에 읽기를 더하는 것을 막는다.
- 관찰 기준(#1191): `order_observed`(`.foragent/order-observed.json`)는 명시적 preread가 보여 준 값 또는 성공 쓰기 직후 값만 저장한다. 새 자식은 `_create_owned(on_created=...)`, 기존은 `update_target`. stale 검사는 clean preread 뒤·소유 라벨/파일/GitHub 쓰기 전에 있다. `decisions.with_marker`는 저장 앞이어야 자기 표식을 사람 변경으로 오인하지 않는다. 고정물이 수동 편집을 흉내 내면 먼저 preread로 기준을 갱신한다. `ParsedIssue`는 7개 필드이고 레인·의존은 없다. statement 이슈의 preread는 에이전트 턴 없이 현재 필드를 돌려준다.
- preread 기록: `order._record_preread`가 `.foragent/preread.json.lock`(O_NOFOLLOW + flock)을 잡고 다시 읽기·추가·교체만 한다(#1189). 저장 뒤 `order_observed.save`를 유지한다. 임시 쓰기는 `write_text`로 둔다(잠긴 시험이 멈춤 지점으로 씀).
- preread(#237, #1012, #1108): 관문은 `order.require_clean_preread`, `tests/test_order.py`는 autouse로 끈다. 대상은 `order.preread_target`(edits도, key `#N`). 해시는 실행기·모델과 계획 전체를 묶는다. 설정은 `run_implementation.lane_config` + `implementer_setup`을 구현과 함께 쓴다(`test_preread_setup_matches_the_implementation_setup`). 새 레인 이슈는 preread·승인을 거절한다(`--approve` 없이 발행 → preread → 재발행). 답에 APPROACH/DECIDE/QUESTION 표시가 하나라도 있으면 `_unknowns`는 QUESTION만, 표시가 없으면 모든 줄이 unknown이다(`order._TAG`). 실행기 `read_only=True`는 codex `-s read-only`, claude `--permission-mode plan`.
- 넘김: `order.lane_proposal`은 본문 비교 전에 생성 표식(결정·자식·소유)을 지운다. 사람이 쓴 내용은 지우지 않는다(#1014 GitHub 링크). 증거에는 참조 내용까지 묶는다. 본문에 줄을 덧붙이면 이런 리더를 모두 고친다.
- 본문·edits: `order_files.issue_body`, 파서 `issue.py`, 스키마 도움말 `cli.py`를 함께 고친다. edits는 부분 덮어쓰기가 아니다(goal·out_of_scope·done_when 필수). 같은 주문의 자식과 edits가 겹치면 거절하니 시험은 새 주문 `id`를 쓴다. 파일 링크는 `order_references.repository_link`(기존 시험 링크 글자가 바뀌면 승인이 풀림). `interactive`는 값 변경 시 approved/ready를 해제한다. `tracker.sub_issues` 한 번 읽고 빠진 자식만 add한다(#988).
- 의존 간선(#1178): `order._reconcile_dependencies`가 실제 간선과 요청을 비교하고 변경 전 approved·ready를 해제한다. `edits[].blocked_by`는 같은 계획 key만, `[]`는 지움.
- 대기(#1178): 본문 `## Waiting`(`order_waiting.section`). `stamp`가 쓰기 직전 변화를 id와 함께 본문에 남기고 `announce(tracker, n, body, language)`가 그 id의 댓글이 생길 때까지 남긴다. `mark_write`는 쓰기마다 새 id를 넣어 revision이 이번 쓰기를 가리키게 한다.
- 소유: [#1003 team identity](team-identity-1003.md). 문맥(#1006): `authenticated_login()` 한 번 + `ownership.effective_orderer`로 decisions·other_orders를 나눈다. 계정을 못 읽어도 라벨 없는 레거시 질문과 답은 유지하고, 모순·미확정은 `owner_error`로 남긴다.
- 맥락 키를 더하면 `test_context_all_sections_and_gate_groups`와 `docs/impl/p8a-order-tools.md`를 고친다.
- 실행 파일 검사(#1206): `order_executables.check`가 `processes._child_environment(root)` 그대로, `.venv/bin`을 더하지 않고 validate_plan 직후 돈다. 줄별 오류는 `DoneWhenExecutableError.lines`. 고정물은 bare pytest 대신 `uv run pytest`가 필요하다.
- 결정 연결(#1025): `decisions.py`(표식 `<!-- foragent:decisions [...] -->`, 생략=유지, `[]`=해제, MARKER는 LF/CRLF 공용, 재사용은 MARKER_LINE). spec 참조는 부모에만, 자식은 plan-child와 부모 map만 쓴다. `fa decisions`는 `state_observations.latest`만 읽고 outbox를 해석하지 않는다. 관찰·쓰기 순서는 `state_observations` ingest 소유이니 옛 `state_observe`를 되살리지 않는다. `acceptance=` 맵은 자식과 edits 번호를 모두 담는다. D 번호 정규식은 양쪽 경계를 `[A-Za-z0-9_]`로 막는다.

## 묶음 발행 (`order_notes`, `order_submissions`, `order_flush*`)

- 노트(#1262): `order_notes.py`, `StateStore.root/notes.jsonl`, 잠금 없는 O_APPEND 단일 write+fsync. 앞쪽 줄바꿈이 중단된 꼬리를 분리한다. 대체도 새 항목이다.
- 주문 보류(#1263): `order_submissions`가 `orders.jsonl`에 원문과 승인 선택을 append한다. 분기는 reviewed-tests·clean-preread 뒤, owner/Git/라벨 쓰기 앞.
- flush(#1264): `order_flush.py`, 저널 `order_flush_journal.py`(`flush-journal.jsonl`, selected→committed→pushed→issues→done). publish_lock → update_base → 전체 `_preflight` → selected 순서. 상태 키는 주문 id가 아니라 로그 행 바이트 위치. 기존 파일만 가리키는 Done when 시험도 보관 행 files에 담는다.
- 복구(#1312): `order_flush_recovery.py`. 원격 전체 이력에서 배치 제목을 먼저 찾고(`--fixed-strings --grep` 뒤 완전 비교), 찾으면 커밋 없이 HEAD만 맞추고 바로 복귀한다. 끊긴 rebuilding은 `read-tree -m -u rebuilt old`로 되돌린다. 재구성은 rebuilding 기록 → read-tree → update-ref → rebased. 커밋된 배치의 이슈 단계는 `_publication_checks(landed=True)`로 기준 검사를 건너뛴다(자기 커밋이 HEAD를 옮겨 영원히 거절됨). landed는 stale 검사 전에 복귀해 끊긴 사이 사람 편집을 덮을 수 있다(알려진 한계, 시험 없음).

## 시험 먼저 (`order_prepare*`, `order_reviewed`, `order_scratch*`)

- 흐름: `order_prepare.py`(작성·기록 `store.root/prepare/<주문id>.json`, 잠금 `.lock` LOCK_NB, 바쁘면 waiting), `order_prepare_prompt.py`, `order_prepare_review.py`(검토자), `order_prepare_cycle.py`(주기·보류·계획 반영), `order_prepare_owner.py`(주인·입장), `order_prepare_setup.py`(맥락), `order_reviewed.py`(발행 관문, 기록을 읽기만 함), `order_scratch.py`·`order_scratch_context.py`(사본). 인수 고정물 `tests/acceptance/lane_tests_case.py`, 단위 시험은 `lane_case` 재사용(`sys.path`에 acceptance).
- statement 이슈: `issues[]/edits[].statements`가 경로를 고른다. `done_when`이 아직 없는 파일을 가리켜도 validate 통과, preread 제외. 새 경로를 열면 "준비 끝"을 가정한 레거시 검사를 모두 찾는다. 경로는 한 번 타면 유지한다(`require_route_kept`, 번호·부모 지도·자식 표식 `_child_prepared`·본문 `issue.has_behaviour`로 판정). 새 주문의 새 이슈는 같은 키라도 새 이슈다.
- 사본: `order_scratch.build`는 임시 GIT_INDEX_FILE로 `read-tree`+`checkout-index`(git archive 아님). 맥락은 원천 폴더째 `copy_tree`(`.claude/agents`, 레인 메모리, 선택 스킬, 새 레인 라이브러리 스킬). 복사 뒤 링크 대상 내용이 원본과 같아야 하고 아니면 질문. 링크 해석은 `order_scratch.real`(3.12 `resolve()`는 순환에서 RuntimeError). 쓰기 경계는 작성 뒤 `lstat` 전체 검사(`changes`)다. 고쳤다 되돌리기는 inode·ctime 도장. 권한은 만든 뒤 `chmod`로 맞춘다(umask, macOS 링크 권한).
- 작성자 격리: Codex `codex_command(isolated=True)`, `--disable hooks`, `-c notify=[]`, `-c mcp_servers={}`. Claude `--permission-mode dontAsk`, `--setting-sources project --strict-mcp-config`, sandbox, `disableAllHooks: true`, 사본의 실행기 설정 삭제(`EXECUTOR_SETTINGS`). `--bare`는 구독 인증이 깨진다. 입장은 `capacity.hold_workload(store, config, kind=..., role=...)`이고 검토자는 `tests=True`, 남은 칸은 `len(machine_holders(store, now, live=True))`(#992).
- 검토(#1048): 시도마다 `review-N/tree`를 새로 만들고 뒤에 `_integrity`로 확인한다. 통과 답은 `_evidence_gaps`(문장마다 쓴 파일을 가리키는 tests·break_it, 명령마다 red) 검사. 빠지면 interrupted, 0 값은 hold. `holds()`는 기록된 status=="hold"만 센다. 재사용과 반영은 정확히 같은 기준 커밋에서만.
- 주기 identity: 요구·스펙·계약·참조·실행기 설정 전체, `context_digest`(레인 메모리 제외), `published_digest`, 다른 이슈 계약(`other_issues`). 다른 statement 이슈의 준비된 시험은 identity에 넣지 않는다(고리). `other_candidates`로 사본에 넣고 `entry["others"]`에 digest만 기록해 검토만 다시 한다. 보류 수·질문은 `requirement`로 묶고, 질문은 이 이슈 동작(`cycle.behaviour`)이 바뀔 때만 다시 연다. 작성자·검토자 사본은 `scratch_files`로 발행 결과와 같게 한다.
- 관문: `require_reviewed_tests`, `require_reviewed_bytes`(acceptance·done_when 파일을 written 또는 기준 커밋과 비교), `require_tested_base`(잠금 전과 `update_base` 뒤, 기준은 실제 얹히는 커밋). 검사는 `publish`가 처음 읽은 `files` 객체로 한다. 준비는 에이전트 시작 전에 `record["claimed"]`를 쓴다.
- 계획 파일 쓰기는 `replace_unchanged`(옮겨 두고 비교 → os.link → 다시 비교)이고 지우기 전에 descriptor로 늦은 쓰기를 본다. 다른 내용은 `<이름>.conflict-*`로 저장한다.
- `order_prepare_owner`: 기록 `owner`={pid,start_time}. `take`가 끝나지 않은 시도를 잠금 안에서 닫고 `project`는 status에서 보여 주기만 한다. 입장 전 TimeoutError는 waiting이다(OSError 하위라 기존 except가 잡는다).

## 인터뷰·설정·기타

- 설정 필드: `Config`에 기본값, 검증은 `config._validate`. `test_exact_defaults`, `test_context_all_sections_and_gate_groups`를 갱신한다. 새 설정 검증 오류도 등록부 + `values["language"]`. `legacy_owner` 검증도 같은 자리이고 `config_validation.py`는 memory 이슈 몫이다.
- 인터뷰: 분기는 `fa order context --json`의 `interview.engine`. 기본 `native`, Ouroboros 시험은 `Config(interview="ouroboros")`를 명시한다. 설치 정보는 `~/.claude/plugins/installed_plugins.json`의 `ouroboros@` 키이고 손상 시 미설치. native 기준은 D58이다. 불명확한 충돌만 Change/Keep/Exception으로 한 번 묻는다.
- 외부 계정 없는 문맥 검증: `order.context(root, config, FakeTracker())`.
- fa-order Change 단계는 같은 승인 주문에서 decisions.md에 날짜·이유·Supersedes 항목을 덧붙이고 `docs/rules.md` 해당 줄을 바꾼다(#1054). D71 경계 문구는 fa-orchestrator `SKILL.md`, `references/order-card.md`, fa-order `SKILL.md` 세 곳에 있다(#1192). GitHub 쓰기는 fa 명령으로, 없으면 그 명령을 요청하는 issues[]를 제안한다(#1234). 스킬 문서에 gh 쓰기 글자를 반례로도 쓰지 않는다. lightest fix 1번은 `MEMORY.new.md` 한 줄 덧붙이기다(#1187).
- `fa profile set`·`fa order progress`는 GitHub 초기화 전에 처리한다. 진행은 `StateStore.order_progress()`(chunk당 다섯 칸). `new_lanes[].skills`와 `fa lanes skills`에는 사람이 고른 스타일만 둔다.
- 사람은 `/fa`만 친다. 스킬의 다음 단계 안내는 `/fa`로 쓴다(잠긴 예외: fa-init의 `fa adopt ci` 뒤 `/fa-order`). 디자인은 "디자인 인터뷰"라고 말한다. 사람용 새 문구는 스킬이 아니라 `messages/*.py`와 CLI JSON으로 넘긴다.
- 관점 자리를 더할 때: `order_perspectives.SEATS`·`DEFAULT_EXECUTORS`·`SEAT_FOCUS`, `messages/order_perspectives.py`, fa-order 5b, `docs/gate.md`, 단위 시험. 원장은 `store.root/perspectives/<plan_hash>.json`.
- `fa settings`: `settings.py`·`settings_write.py`. `load_config`를 쓰지 않고 쓰기 전 사후 조건(`_saved`)을 본다. 주석을 지킨다. `ruff --fix`는 기존 파일의 import를 펼쳐 줄 한도를 깬다.
- `fa init`: 전역 설정 쓰기는 `config._write_global`. 시험 뒤 실제 파일이 아니라 격리 HOME에만 썼는지 본다. 첫 주문 제안은 `commands/init.py::_offer_starter`.
- upstream 보고(#419): traceback은 자유 글이니 구조 줄만 읽고 검증한 값 그대로 보고한다. 실제 traceback은 이슈 로그와 `tick-failure.log`에 있다. 보낼 글 ID는 내용 해시다. 검색 결과가 불완전하면 새 이슈를 열지 않는다. 명령의 모든 실패는 자기 명령 안에서 등록부 키로 바꾼다.
- `fa orch ensure`는 `commands/_orch_box.py::_ensure`이고 `orch_spawn.ensure_orchestrator`를 모듈 속성으로 부른다. 범위 밖 게이트 spawn 경로를 건드리지 않는다.
- 출시 초안은 `docs/launch/{geeknews,x,threads,hn}.{ko,en}.md`, 첫 줄 `# draft, not posted`. X는 한글 2자·URL 23자 가중치로 280 이하. README 자체 제작 수치는 `git log --first-parent --oneline | grep -cE '\(#[0-9]+\)$'`.
- 스펙의 `## Proposed skill edits` diff는 `.foragent/`에 뽑아 `git apply`한다(참조 패치는 `--- a/ +++ b/` 머리줄이 필요). 디자인 참조는 `order_references.py`이고 선택이 `acceptance_blobs`에 들어가 바뀌면 승인이 풀린다.
