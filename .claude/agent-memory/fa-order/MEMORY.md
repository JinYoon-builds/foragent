# Order lane 기억

## 설정
- 설정 필드를 추가할 때 `Config`에 기본값을 넣으면 `DEFAULTS = asdict(Config())`와 알 수 없는 키 검사에 반영된다. 타입·허용값 검증은 `config._validate`에 둔다. 로컬·전역 설정은 같은 검증을 사용한다. 함께 고칠 시험: `tests/test_config.py::test_exact_defaults`, `tests/test_order.py::test_context_all_sections_and_gate_groups`.
- 인터뷰 기본값은 `native`(#109). Ouroboros 폴백 시험은 `Config(interview="ouroboros")`를 명시한다. 설치 정보는 `Path.home() / ".claude/plugins/installed_plugins.json"`의 `ouroboros@` 키 첫 항목 버전이다. 손상되면 미설치, 버전 고정은 문자열 완전 일치, `None`만 고정 없음.
- `profile`은 매핑 또는 `None`이며 주문 컨텍스트 최상위에 그대로 나온다. `fa profile set --kind ... --stage ... --priority ...`은 Git/GitHub 초기화 전에 처리하고 `config.set_profile`이 로컬 YAML만 갱신하며 `date.today().isoformat()`을 기록한다.
- 전역 설정 쓰기는 `config._write_global`. upstream 동의 키는 `upstream_reports`(None=안 물음).

## 주문 컨텍스트·발행
- 외부 계정 없이 검증하려면 `order.context(root, config, FakeTracker())`를 쓴다. 맥락 키를 더하면 `docs/impl/p8a-order-tools.md` 목록과 `test_context_all_sections_and_gate_groups`도 고친다.
- 사실을 판정 boolean으로만 줄이면 리뷰가 "맥락 누락"으로 거절한다. 에이전트가 고를 일이면 원자료를 `fa order context`에 넣는다(#848 `pull_requests`: 열린 PR + 최근 병합 30개, `foragent` 표시).
- 주문 스킬의 인터뷰 분기는 `fa order context --json`의 `interview.engine`을 따른다. `interview.reason`은 native 전환 설명에 쓰고, seed도 실패하면 기존 답변을 유지한 채 AskUserQuestion으로 이어간다. seed는 3단계 되짚기와 4단계 분할의 확정 맥락이다.
- `validate_plan`은 `issues`와 `edits`의 `out_of_scope`에 비어 있지 않은 문자열 검증(`_text`)을 하고, `publish`는 dry-run·외부 작업 전에 이를 부른다. 정상 발행 시험 데이터에도 제외 범위를 넣는다(#110).
- 선택 `files`는 예상 변경 경로/glob 목록이다. `_strings`와 `repo_path`로 검증하고 `## Files` Markdown 목록으로 보존한다(#144). `interactive`는 선택 boolean(기본 false), 신규·재발행 때 라벨을 동기화하고 값이 바뀌면 approved/ready를 해제한다. edits는 기존 라벨을 유지한다(#161). 본문 필드를 더하면 파서 `src/foragent/issue.py`와 스키마 도움말 `src/foragent/cli.py`를 함께 고친다.
- 기존 주문의 자식을 `edits`로 고치는 시험은 새 주문 `id`를 쓴다. 같은 주문의 자식과 `edits`가 겹치면 발행이 거절된다.
- 발행 Git 단계는 `order._publish_files`, `git.publish_lock()` 안에서 `git.update_base(branch)`(fetch 후 ff-only)를 먼저 한다. 가짜 Git(`tests/test_order.py::PublishingGit`)에 메서드를 추가해야 한다(#259).
- `fa order progress <step>`은 GitHub 초기화 전에 처리하며 로컬 기록과 JSON 출력만 한다. 뷰 열기는 `/fa-order`가 `start` 직후 `fa gate watch --open`으로 한다. 준비 사실은 `StateStore.order_progress()`(`order-progress.json`), `update_order_progress`는 같은 잠금 안에서 읽기·수정하고 원자적으로 저장한다. chunk당 다섯 칸(goal/out_of_scope/done_when/lane/skills), `end`는 준비 기록만 지운다(#159).

## preread (#237)
- `fa order preread` 결과는 저장소 안 `.foragent/preread.json`(자체 `.gitignore` `*`)에 둔다. 관문은 `order.require_clean_preread`이며 `tests/test_order.py`는 autouse로 끈다. 실행기 `read_only=True`는 codex `-s read-only`, claude `--permission-mode plan`.
- preread와 승인 관문은 `order.preread_target`을 함께 쓴다. edits도 대상이다(key `#N`). 증거 키에는 계획 전체(정렬한 JSON)가 들어가 어떤 이슈가 바뀌어도 이전 결과가 무효다. `--approve-numbers`에 아직 매핑 안 된 번호가 있으면 이번에 만들 자식 전부에 깨끗한 preread를 요구한다.
- preread는 `run_implementation.lane_config` + `implementer_setup(config, lane, tier, worktree)`를 구현과 함께 쓴다(첫 시도 tier = 이슈 크기). 필드를 따로 흉내 내면 리뷰마다 어긋남이 새로 나온다. 비교 시험: `test_preread_setup_matches_the_implementation_setup`.
- 이번 주문이 만드는 레인(`new_lanes` 중 설정에 없는 것)의 이슈는 preread와 승인 모두 거절한다. 순서: `--approve` 없이 발행 → preread → `--approve`로 재발행. 모르는 점은 스킬이 스스로 답하지 않고 사용자에게 묻는다.

## 관점 (`fa order perspectives`, #414·#745)
- 코드는 `src/foragent/order_perspectives.py`(CLI `commands/order.py`). 원장은 `store.root/perspectives/<plan_hash>.json`(시험이 작업 트리 불변 검사). 시험이 `executors.executor_for`와 `shutil.which`를 모듈 속성으로 바꾸므로 `from ... import`로 묶지 않는다. 질문 id는 `hash[:12]-seat-번호`.
- 자리를 더할 때 고칠 곳: `SEATS`·`DEFAULT_EXECUTORS`·`SEAT_FOCUS`, `messages/order_perspectives.py`의 `perspectives_seat_<자리>`, fa-order 5b 자리 목록, `docs/gate.md` perspectives 줄, `tests/test_order.py` 단위 시험(자리 수는 `len(SEATS)`). 단위 시험은 CI가 잡으니 `fa test tests/test_order.py`를 꼭 돌린다.

## 사람용 글·언어 (리뷰가 매번 잡는 종류)
- 사람이 보는 모든 문자열은 `src/foragent/messages/*.py` 등록부와 설정 언어를 따른다: 실패 사유, PlanError·ConfigError(설정 검증은 `values["language"]`), argparse `help=`, 보고서 본문, 출력의 자리 이름. "기존 help는 영어" 반박은 받아들여지지 않는다. 스킬에 새 문구를 박지 말고 CLI JSON으로 넘긴다(예: `brief.next_step`).
- 에이전트가 만드는 사람용 글(질문 등)에도 언어를 넘긴다: 프롬프트에 "Write every question in <언어>", 결과에 `language` 기록. 에이전트 프롬프트 자체는 영어로 두고 REBUTTAL에 적는다.
- 명령의 실패는 자기 명령 안에서 등록부 키로 바꾼다(`ReportError(key, **values)`). `cli.main` 공통 처리로 새면 영어가 나온다. 공유 모듈(tracker 등)에 새 영어 오류를 넣지 말고, 기반 도구 원문은 `{error}`로만 덧붙인다.
- 파서 생성 중 `cli.load_config`를 부르면 `tests/test_cli.py`의 가짜 load_config 기록이 깨진다. 도움말 언어는 `config._read_yaml(cwd/.foragent.yml)` 또는 `foragent.config.load_config`로 직접 읽고, 하위 parser의 `parse_known_args`를 감싸 `--repo-root`를 먼저 읽는다.
- 한국어 메시지·초안은 해요체(`니다` 금지, `test_voice.py`).

## `fa init`에 질문 더하기
- init 질문을 더하면 질문 수를 세거나 `input()`을 금지하는 잠긴 시험이 깨진다(`test_ci_mode.py`, `test_statusline_wrapper.py`, `test_irreversible_paths.py`, `test_quota_executors.py`, `test_board_status_groups.py`, stdout 정확 비교 `test_cli_split.py`, `tests/test_cli.py`). 먼저 `grep -rl builtins.input tests | xargs grep -l '"init"'`로 전부 찾아 돌리고, 깨지는 잠긴 시험은 한 번에 모아 spec-question으로 넘긴다(#419·#848에서 세 번씩 나눠 물었다).
- Starter order 질문은 `commands/init.py::_offer_starter`, 자격은 `starter.eligible`, 트래커는 `InitReport.tracker`(compare=False). 시험 가짜 report는 SimpleNamespace라 `getattr`로 읽는다(#848). `checks`가 없으면 `gh repo view`로 공개 여부를 묻고, pytest 명령이 없으면 묻지 않는다(`ci_workflow.initial_checks`).

## upstream report (#419)
- traceback은 구조 줄로만 읽는다: 마지막 `Traceback` 블록의 들여쓴 `File "...", line N, in f` 줄과 그 뒤 column 0 첫 줄. 소스 줄은 해석하지 않는다. 패키지 프레임은 `resolve()` 뒤 실제 패키지 안 파일 + 함수 정의 확인, 함수는 점 없는 식별자, 줄 번호는 파일 줄 수 안, 정규식은 토큰 전체(`\S+`). 보고하는 값은 검증한 값 그대로 쓴다.
- 실제 traceback은 flag 이유가 아니라 이슈 로그(`store.log_path(N)`)와 `tick-failure.log`에 있다. 로그에는 사용자 시험 traceback도 섞이므로 검증된 Foragent 프레임이 있는 마지막 블록만 쓴다. 한 줄로 편 event는 같은 이슈의 flag/run.detail 원본을 찾아 같은 규칙으로 읽고, 없으면 후보가 하나일 때만 받는다.
- 중복 판정은 덧글도 본다(`search_issues`가 text-match 헤더·per_page=100, 제목·본문·`text_matches`에서 서명 확인). `incomplete_results`인데 일치가 없으면 새 이슈를 열지 않는다. 가짜 gh는 search/issues와 create/comment만 받는다. 보낼 글의 ID는 내용 해시여야 한다(`load`가 재계산).

## 기타 기능 위치
- `/fa`가 유일한 사람 입구다(`skills/fa/SKILL.md`가 `fa brief --json` → 라우팅). 다른 스킬의 다음 단계 안내도 `/fa`로 쓰고, `/fa-design-session`도 사람에게는 "디자인 인터뷰"라고 말한다. 예외: `test_fa_init_skill.py::test_brownfield_steps_are_present_in_order`는 fa-init에서 `fa adopt ci` 뒤에 `/fa-order` 문자열을 요구한다(#119).
- `fa orch ensure`는 `commands/orch.py::_ensure`가 이유를 먼저 가리고 명시 spawn은 `src/foragent/orch_ensure.py`(잠금 busy `_explicit_lock`, `why` 목록). 시험이 spawn 함수를 바꿔치기하므로 모듈 속성(`orch_spawn.ensure_orchestrator`)으로 부른다. 게이트 spawn 경로(기본값)는 범위 밖이면 건드리지 않는다(#133).
- `fa brief` 점수표 한 줄은 `orch_scorecard.worst_line(collect(store))`, 이름표는 `messages/orch_scorecard.py`의 `scorecard_label_*`(#227).
- `fa role add` 플래그는 `--scope --signal paths:<glob> --skill --why`(test_role_source.py). 문서 순서 시험(`text.find`)은 제목 낱말도 세므로 제목에 검사 구절을 쓰지 않는다(#773).
- README 자체 제작 수치는 `git log --first-parent --oneline | grep -cE '\(#[0-9]+\)$'`와 첫·마지막 커밋 날짜(포함 일수)로 센다(#849). 출시 초안은 `docs/launch/{geeknews,x,threads,hn}.{ko,en}.md`, 첫 줄 `# draft, not posted`, 수치는 README에서만, X 글은 한글 2자·URL 23자 가중치로 280 이하(#850).
- `/fa-order`의 `new_lanes[].skills`·`fa lanes skills`에는 사람이 고른 고정 스타일만 둔다. 도구·검토·분야 스킬은 레인 에이전트가 이슈마다 고른다. `fa skills add URL --json`으로 미리 보고 `--approve --role SKILL=ROLE`로 등록한다. native 인터뷰 기준은 `docs/decisions.md` D58과 보강 2(#111). 대화가 많은 일은 D72대로 표시하고 시각 디자인은 디자인 세션을 제안한다(#161).
- 스킬을 다시 쓸 때 `files`, `interactive`, 시작 직후 뷰 열기, progress 전체 단계를 유지한다(#111).

## 시험·검증
- 러너는 Done when, 인수 기준선, 수정한 파일의 자체 시험만 돌린다(D81). 전체 `fa test tests/`는 10분 제한을 넘으니 관련 파일만 돌린다. 인수 시험은 고치지 않는다.
- 빠른 검증 묶음: `uv run pytest -q tests/test_order.py tests/test_config.py tests/acceptance/test_fa_order_skill.py tests/acceptance/test_fa_order_interview.py tests/acceptance/test_order_progress.py tests/acceptance/test_issue_files.py tests/acceptance/test_interactive_flag.py tests/acceptance/test_publish_own_paths.py`.
- 모든 시험은 conftest가 HOME/XDG/Git 설정을 격리한다(#655, 2026-10-27). 시험마다 HOME fixture를 따로 둘 필요가 없다.
- `src/foragent/*.py`와 `commands/*.py`는 300줄 한도다(`test_cli_split.py`, BASE_MODULES 밖도 `test_scheduler_split.py`). 모듈을 키우면 두 시험을 돌리고 넘으면 도우미 모듈로 나눈다(`upstream_traceback.py`, `orch_ensure.py`). 힌트가 "네 diff 탓 아님"이라 해도 실패 메시지의 모듈 이름을 먼저 본다.
- `.foragent/acceptance-exclusions.json`은 러너(`ci_mode.stage_exclusions`)가 기준 커밋 실패 시험으로 만든다. 리뷰가 "내 시험 제외"로 지적하면 REBUTTAL로 답한다. 리뷰가 다른 이슈의 파일을 지적하면 `git diff origin/main -- <파일>`이 비었는지 보고 REBUTTAL로 답한다.
- 재시도 때 작업 트리가 다시 만들어지면 `.foragent/TRY.md`도 다시 쓴다. 기준선 비교 임시 파일도 저장소 밖(/tmp)에 쓰지 않는다.
- 잠긴 시험의 숫자·질문 수가 새 요구와 부딪히면 spec-question으로 넘긴다. main에서 고쳐 준다(#237·#419·#745·#848).
- uv 캐시를 못 쓰면 `UV_CACHE_DIR=/tmp/...`. 네트워크도 막혔으면 기존 `wheels-v6`, `simple-v21`, 필요한 `archive-v0`를 임시 캐시에 복사하고 `UV_OFFLINE=true uv run pytest`.

## main 따라가기 (리뷰가 "인수 시험 삭제"로 거절하지 않게)
- 재시도를 시작할 때마다 `git fetch origin && git diff --name-only HEAD...origin/main`을 본다. 내 변경과 겹치지 않는 파일은 zsh `files=(${(f)"$(...)"})`로 나눠 `git restore --source=origin/main --worktree -- $files`로 가져온다. main에서 새로 생긴 파일은 `git add -N`, 지워진 파일은 `git cat-file -e origin/main:<파일>`로 가려 지운다. `git hash-object`로 같은지 확인한다.
- 병합 중(MERGE_HEAD 있음)에 main이 또 움직였으면 `git diff --name-only MERGE_HEAD origin/main`만 가져온다. `git checkout --theirs`는 권한이 막히므로 `git restore --source=origin/main --worktree`를 쓴다. 내 파일이 main에서도 바뀌면 `git merge-file`로 3방향 병합한다.
