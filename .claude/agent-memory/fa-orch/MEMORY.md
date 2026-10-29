# orch 레인 현재 메모

## 먼저 볼 함정 (여러 이슈에서 반복)

- 잠긴 인수 시험과 Goal·리뷰가 부딪히는 일이 가장 흔한 막힘이다(#221·#365·#459·#533·#561·#603·#224·#287·#154·#107·#716·#779·#786·#433·#129·#189·#435·#413). 둘 다 만족하는 구현이 없음을 확인하면 시험을 고치지 말고 바로 QUESTION(선택지+추천, main 소스로 재현한 실패 ID 첨부)을 낸다. 리뷰어도 `SPEC-CONFLICT:` 줄로 같은 정지를 낸다(#673, `run_review.spec_conflicts()`).
- 리뷰 지적의 "Broader class … also inspect …"는 그 한 줄이 아니라 같은 종류 전부를 고치라는 뜻이다. 한 자리만 고치면 다음 라운드에 이웃 사례가 나와 3라운드 보류가 된다(#123·#370·#415·#466·#154·#189·#333·#420).
- 같은 영역에서 보류가 두 번이면 지적 자리를 고치지 말고 상태마다 쓰는 쪽을 하나로 다시 정한다(D95, 제품 스킬 `fa-state-design`). 사례: #370 정지는 tick 잠금 아래 얇은 stall-kill 호출 하나로 끝났고, #415 수면은 tick의 `sleep_credit.credit()` 하나만 기록하게 해서 끝났고, #154 대화 생존은 공유 함수 하나가 open·closed·unknown을 돌려주고 구조화된 증거가 있을 때만 closed로 본다.
- 리뷰가 반복해 잡은 결함 종류: 모름을 없음·성공으로 보기(프로세스 표 PermissionError, 읽지 못한 plist, 빈 tmux 응답), 검사 뒤 변경(check/use 경쟁, 잠금 밖에서 읽은 기록으로 삭제 — 잠금 안에서 다시 읽는다), 잃는 변환(UTF-8 대체 문자, `strip()`된 경로, `--ignored=matching`이 접는 디렉터리, Git 모드 비트), 재시작 뒤 같은 식별자 재사용(#238 시도 번호, #413 상속된 `FORAGENT_SESSION_ID`, #154 옛 세션 ID), 메시지가 CLI 옵션으로 읽힘(#154: 사용자 문구는 `--` 뒤에 둔다), 감시기 정지 결과를 소비자마다 다르게 처리(#435: 모든 `run_related_check` 호출자가 같은 stalled-restart 규칙을 따라야 한다; 런타임 초과 뒤 일시 오류는 진행 없음의 증거가 아니다), 가드 뒤의 변형(#466: staging·clean filter 같은 변경 작업 뒤, 리뷰 전에 다시 검사).
- 줄 수 상한: `test_scheduler_split`이 `scheduler.py` 250줄·분할 모듈 300줄, `test_cli_split`이 `src/foragent/*.py`·`commands/**` 300줄(`cli.py` 150), `test_runner_split`이 run_*.py 600줄을 건다. main 자체가 상한에 붙어 있거나 넘는 일이 잦아(scheduler.py 249, scheduler_recovery.py 300) 한 줄만 더해도 "Acceptance baseline regression"이 난다(#318·#129·#130·#786). 편집 전에 `wc -l`로 재고, 기존 줄을 접어 순증 0으로 맞추거나 helper 모듈로 뺀다. tick 끝 작업은 `scheduler_housekeeping.after_tick`·`orch_spawn.deciding()` 같은 래퍼에 넣는다. 바꾼 파일마다 `tests/acceptance/test_*split*.py`를 전부 돌린다.
- `skills/fa-orchestrator/SKILL.md`는 `test_orch_skill`이 splitlines 150줄 이하를 강제한다(끝 줄바꿈이 없어 `wc -l`은 149). 16개쯤의 인수 파일이 본문을 정규식으로 읽는다(단어 순서·백틱까지). 늘릴 땐 시험이 안 읽는 설명을 `references/<topic>.md`로 옮기고 한 줄 링크를 남긴다. 옮기기 전후로 `grep -ln fa-orchestrator tests/acceptance/*.py` 묶음과 helper import로 읽는 시험(question_routing)의 PASSED/FAILED 집합을 비교한다.
- "base kept moving" 타임아웃과 "Too big for one session"은 핫 파일(run_publish·scheduler·runner)을 넓게 건드린 이슈에서 반복됐다(#154·#370·#421·#560·#107·#123). 크기 l 이슈는 검증 단위로 쪼개고, 병합 충돌은 main이 옮긴 함수를 분할 모듈로 옮겨 적는다.
- 리뷰는 새 base와 비교하므로 브랜치 뒤에 base에 생긴 파일을 "삭제"로, 옮긴 파일을 "이동"으로 지적할 수 있다. `git ls-tree origin/main`·`git log HEAD..origin/main`으로 확인하고 오탐이면 커밋 SHA를 REBUTTAL에 남긴다.
- 2026-10-28~29: `invalid literal for int() with base 10: 'dream-adopt'`로 gate 실행이 error가 됐다(#466·#772·#435). 실행 기록 키가 언제나 이슈 번호라고 가정하지 않는다(꿈 기록 `dream-*`, [[project-189-lane-dream]]).

## 시험과 검증 방법

- 회귀 판단은 변경 전 소스와 비교한다: 이슈 커밋 이전 SHA를 `git archive`로 풀고 `PYTHONPATH=<archive>/src`로 같은 시험을 돌린 뒤 `foragent.scheduler.__file__`로 실제 로딩을 확인하고 실패 ID 집합을 비교한다. 같은 ID면 REBUTTAL에 근거를 남긴다.
- "Acceptance baseline regression (previously passing tests)"는 GitHub `foragent-acceptance` CI(리눅스) 비교다. 상세는 `gh run list --branch foragent/issue-N --workflow foragent-acceptance` → `gh run view <id> --log`의 "Compare acceptance passing sets". runs/issue-N/*check*.log에는 상세가 없다. 실시간 `os.getloadavg()`·실제 시계를 읽는 시험은 간헐 실패한다(#772 facts, test_cli의 고정 `NOW`와 `cli.datetime.now` — 해당 시험에만 `FixedClock`).
- `tests/conftest.py`는 시험 중 `git commit`/`checkout`을 막는다. 이력은 `git_history.update()`(fast-import), `commit-tree`·`write-tree` 같은 plumbing과 bare `origin`으로 만든다. 실제 창(`FORAGENT_VIEW=off`), 오케 생성, 실제 HOME·XDG_STATE_HOME, 실제 codex app-server, 실제 수면도 conftest가 막는다.
- 이 환경에서 `git checkout <file>`·`--theirs`는 권한 거부된다. `git show HEAD:<path> > <path>`로 되돌리고, 충돌은 표식을 직접 편집한다.
- zsh는 `$files`를 단어로 나누지 않는다. `${=files}` 또는 `$(... | tr '\n' ' ')`로 pytest·`fa test`에 넘긴다.
- 긴 묶음: `tests/acceptance/test_ci_mode.py`는 부하 중 ~30분이라 Stop hook의 check-done이 "Check timed out"을 낸다. `fa test`를 백그라운드로 따로 돌린다. `tests/test_runner.py` 전체는 돌리지 않는다(관련 시험 예산 초과); 새 시험은 분할된 모듈의 시험 파일에 둔다.
- 인수 파일과 같은 모듈명의 단위 시험 파일을 만들지 않는다(기본 import 모드에서 충돌). 경계 시험은 `tests/test_<topic>_unit.py`.
- 새 Config 키는 `tests/test_config.py::test_exact_defaults`도 고친다. 설정 로더는 모르는 키를 거부하므로(config.py) 새 최상위 키는 IGNORED_KEYS에도 넣는다.
- 작업공간이 재생성되면(대상 전환·한도 실패 뒤 재시도) `.foragent/TRY.md`·`REBUTTAL.md`와 미커밋 변경이 사라진다. 다시 쓴다.
- 시험 안 값 확인은 capsys를 우회하는 `os.write(2, ...)`와 `-s`.
- `os.killpg` EPERM으로 `test_tick_cleans_worktree_when_real_runner_is_terminated_between_logged_commands[True]` 등 프로세스 종료 시험이 간헐 실패한다(변경 전 소스에서도 재현). 실제 기계에서 kill 재현은 하지 않는다.
- 기본 uv 캐시에 쓸 수 없으면 `UV_CACHE_DIR=<쓰기 가능한 캐시> uv run --offline pytest`. 오프라인 빌드에 setuptools·PyYAML이 없으면 기본 캐시의 `wheels-v6`·`simple-v21/*.rkyv`·참조하는 `archive-v0/<id>`까지 복사한다.
- `run_case`로 한 시험에서 여러 번 시작하면 주입 시계를 전진시킨다(실행 이력은 `(이슈, started_at)`으로 중복 제거).

## 구조 규칙

- 분할 facade(scheduler·runner·run_publish·state): 분할 모듈은 모듈 수준 `from foragent import scheduler`로 원래 전역 이름을 호출 시점에 `scheduler.X`로 조회해 facade 패치가 닿게 한다. facade는 원래 import를 전부 남기고 재노출은 하단에 둔다(클래스 정의 시점에 쓰는 이름은 상단). [[project-196-state-split]] [[project-560-run-publish-split]]
- `commands/*.py`는 `facade`·`repo_store`만 import한다(test_cli_commands). argparse·gh는 `cli.argparse`·`cli.gh`로 쓴다. GitHub 없이 처리하는 동사는 `INBOX_ACTIONS`에 넣어 Git·GhTracker 준비 전에 분기한다.
- 저장 dataclass는 `records.load_record(cls, data)`로만 적재하고(인수 AST 검사가 `X(**m)`을 막음), 다시 쓸 때는 `StateStore._write_record()`/`records.write_back()`으로 모르는 필드를 보존한다. Run 파일은 `_write_run()` 하나로만 쓴다(#386).
- `_state_lock`은 재진입 불가다. 잠금 안의 기록은 `orch_events.write_event`, 밖은 `append_event`. `start_run`이 `worktree_lock(n)`을 잡으므로 그 잠금을 쥔 채 부르면 교착된다. `hold_tick_lock()`만 같은 스레드의 중첩 `tick_lock()`을 재진입시킨다.
- 요청을 끝내는 동사는 `orch_inbox.set_request_status(..., check=fn)`로 확인과 완료를 state lock 안에서 한 번에 한다(동시 답 경쟁).
- 선택적 JSONL 기록의 부재는 FileNotFoundError와 NotADirectoryError 둘 다다. 공용 `_jsonl`에서 이 둘만 처리한다.
- 공통 실행 루프로 넘길 땐 기존 `GitRepo` 인스턴스를 넘긴다(`local_skill_paths`가 인스턴스에 있음). 작업공간 재생성 직후 `_inject_lane_skills()`를 부른다.
- `update_run`은 `observe=True` 또는 pid·process_identities·executor_pgid가 있을 때만 프로세스 표를 읽는다(#547).
- `run_logged(env=...)`는 부모 환경에 더한다. Claude 세션 공통 환경은 `executors.CLAUDE_SESSION_ENV`(#814).
- 게이트 자식 환경은 `launcher.gate_environment()`. launcher·kick·launchd 단위 시험은 Popen 인자 집합을 정확히 비교한다.

## 영역별 메모

- 차단: gate·runner는 `actions.flag()`를 공유한다(`blocked` 라벨 + `<!-- foragent:blocked reason=... -->`). `format_comment()` 기본은 `needs-human`, `parse_reason()`은 둘 다 읽는다. tick 타임아웃은 GitHub의 최근 blocked 댓글 시각 기준. 복구·실행 직전 검사에 `blocked`를 포함한다.
- 복구: 열린 PR이 있는 runner가 죽거나 멈추면 ready로 돌리지 말고 `retries-exceeded`·`stalled`로 막는다. 첫 stall은 `_kill_run`이 신호 전에 `stalled-restart`를 기록하고 다음 tick이 ready로 돌린다(#305). 그룹 종료는 crash·미완료 stall에만 하고, 완료 결과가 있으면 신호 없이 정리한다. `ProcessLookupError`만 그룹 소멸로 보고 권한 오류는 삼키지 않는다(#84).
- 기록 없는 running 라벨의 `reconciled` 이벤트는 `scheduler_reconcile.orphan_crash_detail()`이 먼저 쓰고 중복은 `orch_events.has_event(..., detail=라벨 세대)`로 판단한다(#130).
- GitHub 한도: `<state>/github-pause.json`에 프로젝트 공통 재설정 시각(늦은 쪽 보존), `tick()`이 가장 먼저 확인한다. `TrackerError.is_rate_limit`는 HTTP 429·한도 진단만(단순 403 제외). 한도 오류를 삼키거나 추가 GitHub 호출로 덮지 않는다(#210).
- 실행기 한도: `executor-pauses.json`, `_available_executor()`가 매 실행 직전 확인. 사용량 파싱은 `usage_events`(`name/used_percent/resets_at`만 저장). 한도 뒤 재시작 시험은 시계를 `limit_wait_minutes` 뒤로 옮긴다. [[project-107-claude-quota]] [[project-318-usage-recheck]] [[project-786-executors]] [[project-186-executor-choice]]
- 힌트 `<state>/hints/<n>.txt`는 `/goal` 감싸기 전에 넣고, 실행기가 일반 결과를 돌려준 뒤에만 소비한다(limit·transient·예외에서는 보존).
- 리뷰: 판정 불가 리뷰는 구현 시도를 하나 쓰고 원문 전체를 `A reviewer said (verbatim):` 뒤에 넣는다(4,000자 꼬리 제한 금지). 반박은 `.foragent/REBUTTAL.md` → PR `## Rebuttals`·다음 리뷰. 규칙 문구를 바꾸면 `tests/runner_support.expected_review_rules`도 바꾼다. 리뷰 관점 체크리스트는 `review_perspectives.with_checklist()`. [[project-533-review-panel]] [[project-226-review-notes]]
- 리뷰 facts(#421): 판정은 `orch-events.jsonl`의 kind=review(파일 mtime 이후 첫 이벤트, `source: orchestrator` 제외)로 읽고, 지적 추출은 공용 `run_review.code_review_findings()`를 쓴다(facts에서 형식 판별을 다시 구현하지 않음). PASS의 path:line도 센다. NOTE 줄은 `review_notes.strip()`·`note_text()`로 뺀다.
- 기준선(D53): 대상 ref의 별도 detached 작업공간에서 캡처, JUnit `junit_family=legacy`로 정확한 ID, 0개면 pytest를 빈 선택으로 부르지 않는다(전체가 돈다). 검증 순서는 Done when → 기준선 통과 ID → 관련 단위 시험(`_related_unit_tests()`, AST import). [[project-123-baseline-survivors]] [[project-483-baseline-follows-base]] [[project-433-baseline-facts]]
- premerge·merge(#31·#152): 정확한 PR head를 detached 작업공간에서, base를 `merge --no-commit`한 결과로 시험한다. `add_detached_worktree()` 성공 뒤에만 정리 범위에 넣는다(디렉터리 존재로 판단 금지). 병합 뒤 재설치는 `orch_reinstall.after_merge()`가 병합된 base SHA의 임시 작업공간에서 한다(#93·#103).
- gh 호출 계수는 `foragent/gh.py` 모듈 기록기, 단계 시간은 `state_phases.close_phase`(#224·#333). GhTracker 재시도는 주입 `sleep`을 쓴다.
- CI 제외 목록은 커밋 trailer로 전달되고 main은 `.foragent/acceptance-exclusions.json`을 추적하지 않는다. 병합 충돌(UD)이면 지운다. 제외 목록에 시험을 더하면 "CI 약화"로 거절된다(#127·#370·#318). [[project-567-ci-exclusions-drift]]
- 감시: `orch.watch()`는 읽기 예외를 `{"unknown": true}`로 내고 `orch-watch.json`은 건드리지 않는다(별도 `orch-watch-unknown.json`). 서명은 blocked·needs_human·merged 번호와 queue_empty만(#32·#217).
- 하트비트: `orch-heartbeat.json`·`gate-heartbeat.json`, tick lock 직후 기록, 30초 이하면 alive(#127). tick 결과는 `tick-outcome.json`을 tick 잠금 안에서 쓴다(#387).
- 레인 대기열 `lane-queues.json`은 `_start_ready()` 직전 ready만 재정렬한다(#235). 계획은 [[project-287-plan]], 교차 레인 순위는 [[project-603-cross-lane]].
- stale Files 보고(#604): 승인 시점 base는 `rev-list -1 --first-parent --until=<ts>`로 찾는다. `ls-tree -z` split 뒤 빈 이름을 뺀다. scheduler에 git 호출을 더하면 test_cli의 엄격한 가짜 git이 깨지므로 대상이 있을 때만 읽는다.
- 로그 보존(#131): `state_logs.py` 믹스인, 묶음 나이는 finished_at, 없으면 묶음의 가장 새 파일 mtime. 보존 일수는 `settings.read_machine()`의 log_retention_days(기본 7). 삭제 결정은 worktree 잠금 안에서 다시 읽은 값으로 한다.
- 역할 호출(#770·#772·#794): `fa orch role <name> --why`는 `orch_architect.call/run_session(role=)`, 아키텍트도 roles 원본에서 agent·skills·memory를 읽는다. 신호는 `orch_roles.observe`/`signal_met`, 요청 상태는 레코드와 `role-signal-*` 이벤트에서 유도한다.
- `fa doctor --live`(#248): 실행기 어댑터를 직접 부르면 "runner 경로 우회"로 거절된다. 임시 StateStore·`FakeTracker`로 RunContext를 만들어 `_prepare_implementation`+`_implement`를 그대로 돈다. clone을 쓰고 worktree를 쓰지 않는다.
- 되돌릴 수 없는 경로(#785): `irreversible_paths.register()`는 `_merge_decision()` 한 곳, HEAD와 `origin/<base>` 트리를 읽는다.
- Windows 조기 종료(#846): `cli.py`의 foragent import 앞 `try: import fcntl`.

## 이슈별 상세 파일

- [#104 skill catalog](project_104_skill_catalog.md) — Available skills prompt section, Skills used in Run/PR
- [#106 machine AIMD budget](project_106_capacity.md) — capacity.py, data-root state, per-repo ceiling, grace window
- [#107 Claude quota keep/burn](project_107_claude_quota.md) — scheduler_quota.py, store clock for expiry
- [#108 orch --say](project_108_say.md) — person line format, view helper outside watch_sections
- [#123 baseline survivors](project_123_baseline_survivors.md) — one exact collect-only listing; never infer ids from probe exits
- [#128 orch spawn](project_128_orch_spawn.md) — gate opens orchestrator; conftest opt-out
- [#129 blocked push](project_129_blocked_push.md) — orch_push hook after spawn, notify-blocked.json re-arm
- [#186 executor choice](project_186_executor_choice.md) — headroom modules, stored pair
- [#189 lane dream](project_189_lane_dream.md) — dream Run records, dream-done event, budget demand
- [#196 state split](project_196_state_split.md) — state_* mixin layout, `_state.X` call-time lookup
- [#226 review NOTE lines](project_226_review_notes.md) — review_notes.py, break-it rule
- [#243 machine-wide test slots](project_243_test_slots.md) — flock slots, waited credit, conftest isolation
- [#287 lane plan](project_287_plan.md) — plan.json, non-reentrant state lock, stall vs 1h clock
- [#318 usage recheck](project_318_usage_recheck.md) — free quota re-read in tick, note_limit baseline
- [#333 orch facts](project_333_facts.md) — read-only facts view, capacity.observing()
- [#365 machine.yml budgets](project_365_machine_yml.md) — single source for shared budget files
- [#366 installed gates](project_366_installed_gates.md) — gates.py discovery, conftest real-HOME isolation
- [#368 ci-not-started](project_368_ci_not_started.md) — CIEvidence.not_started from jobs/annotations
- [#370 orch stop](project_370_orch_stop.md) — thin stall-kill caller under tick lock
- [#376 neutral base moves](project_376_base_move.md) — docs/pending-locked moves skip re-verify
- [#379 risk map](project_379_risk_map.md) — base-ref map read, review/tier/always_human hooks
- [#415 sleep ledger](project_415_sleep.md) — slept_seconds sensor, machine ledger
- [#416 retry fixes](project_416_retry_fixes.md) — retry marker, stop file path, majority-only rebuild
- [#417 update check](project_417_update_check.md) — daily release cache, fa update lock order
- [#420 whole picture](project_420_whole_picture.md) — cuts.py notice, WholeFailure, moved_by on base merges
- [#433 baseline facts](project_433_baseline_facts.md) — shared limit module, membership rule
- [#449 overlap facts](project_449_overlap_facts.md) — file_conflicts in orch_facts_runs, 30-line human cap
- [#450 PR title from issue](project_450_pr_title.md) — retitle before merge
- [#483 baseline follows base](project_483_baseline_follows_base.md) — re-capture baseline on result-affecting base move
- [#497 memory critical line](project_497_memory_critical.md) — critical holds starts only
- [#514 neutral config/locked moves](project_514_neutral_more.md) — config key rules
- [#533 review panel](project_533_review_panel.md) — _panel_review threads+merge, limit re-raise
- [#541 resource facts](project_541_resource_facts.md) — tick sample file, facts resources
- [#560 run_publish split](project_560_run_publish_split.md) — facade + 6 modules, porting main hunks
- [#564 tick step times](project_564_step_times.md) — step/timed contextvar
- [#567 CI exclusions drift](project_567_ci_exclusions_drift.md) — subset rule, slow test_ci_mode under load
- [#590 orch liveness](project_590_orch_liveness.md) — heartbeat session, nearest Claude literally
- [#603 cross-lane start](project_603_cross_lane.md) — _ranked sort key, global-queue.json
- [#650 wake grace](project_650_wake_grace.md) — wake-grace.json, per-run activity ends it
- [#674 SKILL.md line budget](project_674_skill_space.md) — move untested prose to references/
- [#712 merge turn](project_712_merge_turn.md) — FIFO merge-queue.jsonl, turn record before merge_pr
- [#716 role agents](project_716_role_agents.md) — architect prompt/skill split, pstack test pins refactor.md
- [#779 scorecard flow](project_779_scorecard_flow.md) — flow module, binding-events.jsonl
- [#786 ordered executors](project_786_executors.md) — executor_list, fallback order, spend.jsonl
