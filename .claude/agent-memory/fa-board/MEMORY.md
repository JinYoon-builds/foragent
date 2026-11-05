# Board lane 기억

## 보드 (Projects v2)
- 보드의 진실은 이슈 라벨이다. `board.sync_status(ctx, issue, status)`가 저장소 문맥을 잠시 연결하고 공개 함수 `board.set_status(issue, status)`를 호출한 뒤 복원한다. 초기화·동기화 실패는 로그에 남기고 계속한다. 잠긴 인수 테스트가 `set_status`를 인자 두 개짜리로 바꿔치기하므로 공개 모양은 바꾸지 않는다.
- Projects v2 전송은 `Tracker.graphql(query, variables)` 한 곳이다. `GhTracker`는 `gh api graphql --input -`에 JSON을 넘겨 타입을 보존한다. HTTP 성공이어도 응답의 `errors`를 확인한다.
- 보드 소유자는 `repository.owner.id`로 찾는다. 개인·조직 `projectsV2`와 필드 조회 모두 페이지를 순회한다. 저장된 ID가 없어도 같은 소유자·제목의 보드를 재사용한다. 생성 후 필드 설정만 실패해도 ID를 저장해 다음 init에서 보완한다. `--no-board`·`--dry-run`은 Projects API를 부르지 않는다.
- Status는 #184부터 사람용 다섯 칸이다(`board.COLUMNS`/`DEFAULT_MAP`, 상태→칸은 `board.board_column`; 내부 `STATUSES` 일곱 개는 칸으로 묶인다). `ensure_fields`는 이번 호출에서 만든 새 보드(`new_project=True`)에서만 Status를 다섯 칸으로 바꾸고, 기존 보드의 Status 옵션은 건드리지 않는다. 다른 필드(Lane 등)는 기존 옵션 뒤에 새 옵션만 붙인다. `UpdateProjectV2FieldInput.singleSelectOptions`는 목록을 덮어쓰므로 기존 옵션의 id·색·설명을 함께 보내야 카드 값이 남는다([참조](https://docs.github.com/en/graphql/reference/projects#projectv2singleselectfieldoptioninput)). FakeProjects는 삭제된 옵션의 카드 값을 지우는 동작을 유지해야 손실을 드러낸다.
- 팀 보드는 `.foragent.yml`의 `board_map`을 쓴다. init은 남의 보드를 처음 받아들일 때만(`board_id != config.board`이고 `board.ensure(..., created=set)`에 없을 때) 저장하고, 칸이 같으면 묻지 않고 `DEFAULT_MAP`을 저장한다(`test_board_default_is_persisted_and_idempotent`). 막힌 카드의 ⚠는 `blocked` 라벨로 대신한다(소유자 결정). `card_warning()`은 다른 화면용 판정 함수다.
- 같은 칸 안의 이동(Approved→Ready)은 쓰기가 없어야 한다. main에서 들어온 보드 테스트가 옛 상태 이름을 기대하면 기대값을 `board.board_column(state)`로 바꾸고 테스트 이름은 유지한다.
- 닫힌·미승인 이슈는 `sync_status(..., existing_only=True)`로 기존 카드만 옮긴다. 카드 상태는 목록을 새로 읽는 tick에서 `board.refresh_cards`가 보드 전체를 한 번 읽어 맞추고, 카드가 없으면 `board.NO_CARD`를 기록한다. 이슈별로 영구 캐시해 건너뛰면 리뷰에서 막힌다.
- Your turn(#694): `sync_status`에서 열린 이슈가 needs-human·waiting-person 라벨이나 열린 질문(`orch_inbox.waiting_issues`)에 걸리면 Done/closed/merged와 `parent=True`를 빼고 "Needs human"으로 바꾼다. 질문 읽기 실패는 경고만(`test_board_parents`의 가짜 store에 `root` 없음).
- 발행 중 보드 추가의 `TrackerError`는 호출자에게 전달한다. 단 `addProjectV2ItemById`의 `Content already exists in this project`는 성공으로 보고 필드 갱신을 건너뛴다(item ID 없음). 조회·필드 갱신의 같은 문구에는 적용하지 않는다.
- 빈 목록 판정(#163): `GhTracker.list_issues`는 빈 결과를 한 번 재조회하고, 같은 인스턴스의 앞선 목록이나 `StateStore.for_repo(repo).local_issues()`가 근거이면 `TrackerError("...empty listing...")`. 단위 테스트는 `_EMPTY_RETRY_DELAY`를 0으로.
- 집중 검증: `uv run pytest -q tests/test_board.py -k existing_status`, `uv run pytest -q tests/acceptance/test_board_projects.py`.

## 주문 상위 이슈와 기록
- #411: 열린·미승인 이슈(자기 blocked·needs-human 라벨과 무관)에 로컬 `children`이 있으면 `board_parents.follow_children`가 목록을 새로 읽은 tick에서만 `sub_issues`로 칸을 계산한다. 승인된 상위 이슈는 일반 작업이다. 취소된 하위 이슈는 `parent-child-not-planned` 플래그와 orch 이벤트(`Issue.state_reason`). 열린 주문 카드는 `sync_status(..., parent=True)`만 쓴다. 상위 이슈를 닫는 주체는 `follow_children` 하나다(완료 댓글 먼저, not_planned면 열어 둠). 그래서 `_batch_pr` 본문은 `Closes #부모` 대신 `batch_pr_parent` 키(닫기 키워드 없음)를 쓴다. `_reconcile_batches`는 배치 PR 없이 닫힌 상위 이슈도 마무리하고, `_batch_pr`은 닫힌 상위 카드를 "PR open"으로 되돌리지 않는다.
- #705: publish만 하위 이슈 없는 주문(`not plan["issues"] and not mapping`)의 기록을 보드에 넣지 않고 닫는다. tick은 하위 이슈 없는 기록을 닫지 않는다("빈 작업은 완료가 아니다"). 닫힌 이슈 가벼운 읽기는 `RecordingTracker.observe_closed_issues`(watermark `closed_issues.json`, 없으면 `issues_observation()["as_of"]`부터)를 `_reconcile_batches` 맨 앞에서 부른다. 첫 호출에 읽기를 건너뛰거나 캐시 행으로 원격 변경을 거르면 막힌다.
- #837(최종): 기록의 참조 닫기에 상한·캐시·순서 돌리기 없음(오케 결정). 목록 안 참조가 모두 닫히고 하위 이슈가 완료일 때만 목록 밖 참조를 그 tick에 기록마다 새로 `get_issue`.
- "공유 메시지 키" 요구는 같은 일을 하는 모든 쓰기 지점(publish의 기록 닫기 + tick의 부모 닫기)이 한 키를 쓰라는 뜻이다. 옛 키를 남기면 막힌다. 개수 포함 문구는 `{count}` 인자로.
- [issues.json 캐시 (#588)](issue_record_cache.md) — 읽기만 캐시, 잠금 쓰기는 새로 해석; fstat으로 버전 고정; deepcopy 금지
- [닫힌 PR watermark (#589)](closed_pr_watermark.md) — merged_prs.json 목록+watermark, GhTracker만 증분, 성공 뒤에만 기록; state_*.py 300줄 제한

## init
- init은 기존 YAML의 주석·값을 보존하며 키를 더한다. `board: null` 같은 교체에 중복 키를 만들지 않는다. 설정 추가 시 `tests/test_config.py::test_exact_defaults`, init CLI 옵션 추가 시 `tests/test_cli.py`의 kwargs 완전 일치 검사를 함께 고친다. 기존 파일 설정 테스트는 `no_board=True`로 격리한다.
- 기본 도구는 설치가 아니라 로그인으로 판정한다: `codex login status` 종료 코드, `claude auth status` JSON `loggedIn`. 누락·시간 초과·잘못된 응답은 로그아웃. 기존 설정은 재탐지하지 않는다. Claude를 고르면 `reviewer: claude`도 저장한다(아니면 `resolved_reviewer`가 Codex를 고름).
- `tests/test_init.py::setup`은 도구 확인을 subprocess 경계에서 오프라인으로 대체하고 API 키 환경 변수 두 개를 지운다. 잠긴 인수 테스트도 같은 경계에서 주입하므로 초기화 함수를 가짜로 바꾸지 않는다(`tests/acceptance/test_claude_only.py`).
- 사전 점검 문구(#843): `messages/init_preflight.py`. origin 없음/비GitHub는 `remote_slug`의 GitError `__cause__` 유무, gh 미설치/로그아웃은 OSError/CalledProcessError. 기존 시험이 영어 앞머리를 match하므로 ko에도 앞머리를 유지한다. 언어는 `--language` → 기존 설정 → en.
- [init 역할 에이전트 (#717)](role_agents_init.md) — skill은 data-files로 배포, 커밋 실패 복구는 내용 일치 파일만, test_init ALL_PATHS
- [roles 원본 (#769)](roles_source.md) — roles.py 단독 writer, 새 설정 키면 `uv run fa test`. 최종 답(옵션 3): `roles:` 키가 없으면 re-init은 파일을 바이트 그대로 두고 첫 `fa role` 명령이 블록을 쓴다. 그 파일의 "round 12 QUESTION 추천: 항상 이전"과 "every init writes roles"는 이 답으로 대체됐다.

## API 키 Claude (#889)
- 게이트의 `os.environ`에서 파생한다(`scheduler_quota.api_key_billing`, OAuth 토큰 우선). `billed()`가 `entry_for`·`fallback_order`에서 claude를 api로 바꾼다. 실행기 항목을 dict로 직접 만들지 말고 `entry_for`를 쓴다(재개 실행의 kept 항목·남은 implementer 기본값이 billed를 건너뛰어 리뷰에서 막힘; 과금 파생은 승인 경계에서 한다). 상한 없는 api는 `blocked`가 `switch_reason_uncapped`로 막고 `can_review`도 거른다. init은 `claude --version` 성공+키일 때만 사용 가능으로 세고 `InitReport.notes`로 한 줄 안내. `test_quota_executors.py`의 init 시험은 #848 `--starter` 전까지 실패가 기준선.

## 상태 줄과 presence
- `fa statusline`은 Claude Code가 매번 부르므로 `cli.main`에서 parser 전에 분기한다. 설치 질문은 `builtins.input`, EOF/OSError는 "unanswered"로 기억하지 않는다. 비대화형은 `fa init --statusline yes|no`. 집중: `tests/acceptance/test_statusline_wrapper.py tests/test_statusline.py`. settings.json 바이트 보존 편집은 `statusline._member_span`.
- JSON 바이트 편집(#412): 중복 키는 마지막 값이 유효하다. 지울 때 같은 이름을 모두 지우고, 쓰기 전 `json.loads(편집본) == 의도한 dict`가 아니면 쓰지 않는다.
- presence(#754): 로컬 상태만 읽어 사용자 출력 마지막 줄에 붙인다. 시각은 `statusline.datetime.now(UTC)`(잠긴 테스트가 cli·state·statusline의 datetime만 바꿈). 다른 기능의 전제에 묶지 않는다: 이전 명령이 없어도 붙이고 상태가 없으면 0. 겹친 wrapper(NESTED_ENV)만 침묵. 오늘 머지의 출처는 record_merge 하나이고 `presence.gate_merges`가 runs/<n>.json과 이력을 둘 다 읽는다. merged_at >= started_at만 센다. `tests/test_statusline.py`는 tmp로 chdir한다.
- 세션 시작 훅(#755): `fa statusline --session-start` → `presence.session_start_message`(systemMessage JSON), 시작 시각은 `$XDG_DATA_HOME/foragent/presence-sessions/<sha>.json`(유일한 writer; 저장소 store에 두면 repo 없는 프로젝트에서 사라짐). 훅 정리를 wrapper 소유에 묶지 않는다: uninstall은 기록이 없거나 statusLine이 바뀌어도 훅을 지우고, init은 wrapper가 켜져 있을 때만 더한다. 바뀐 게 없으면 다시 쓰지 않는다(inode·mtime으로 시험).
- 저장소 찾기(#802): repo 키가 없으면 `git.origin_slug_from_files(root)`가 .git/config(작업 트리면 gitdir→commondir)를 읽는다. url 규칙은 `git.slug_from_url` 하나. presence는 자식 프로세스 금지. 확인은 `test_statusline_repo_slug.py`.

## 문구
- 새 사람용 문구는 PR 본문 한 줄이라도 `src/foragent/messages/<기능>.py`의 `MESSAGES`에 두고 `from foragent.i18n import text`로 부른다. Files 밖이어도 레지스트리 규칙이 이긴다. 저장소 밖 명령의 언어는 `load_config(Path.cwd()).language`, 실패하면 "en".

## 범위와 소유권
- `scheduler.py`는 `test_scheduler_split.py`가 250줄로 제한한다(지금 250줄, 여유 없음). Files에 없으면 손대지 않고, 새 로직은 board_parents 같은 별도 모듈에 둔다. `test_cli_split`은 src/foragent/*.py 각 300줄.
- 다른 이슈가 주인인 상수는 main에 들어오기 전까지 정의하지 말고 문자열을 쓴다("do not define it here", #694). 인수 테스트가 다른 레인의 미구현 API에 기대면 만들지 말고 blocked-by를 제안한다.
- 잠긴 시험과 이슈 문구가 부딪치면 QUESTION.md로 올린다. 오케가 시험이나 요구를 고친다(#755·#769·#837). 리뷰가 서로 반대 방향(예: "굶주림" ↔ "낡은 캐시")을 번갈아 막으면 구현을 더 정교하게 만들지 말고 바로 QUESTION.md로 요구를 바꾸게 한다(#837에서 3라운드 낭비).

## git과 재시도
- 리뷰가 "내가 안 건드린 파일 삭제"를 지적하면 `git diff origin/main...HEAD -- <file>`(점 세 개)을 본다. 비어 있으면 main이 앞서간 것이다(`--name-status HEAD origin/main`에서 `A`). 겹치는 파일이 없고 HEAD가 origin/main의 조상이면 `git reset --keep origin/main`, 아니면 `git log HEAD..origin/main` 커밋을 `git cherry-pick --no-commit` 후 `git reset -q`(`git merge`는 권한 거부). 그래도 남으면 `.foragent/REBUTTAL.md`로 반박한다.
- 병합 충돌에서 HEAD 쪽에만 있는 테스트는 `git grep -n 'def <name>' MERGE_HEAD -- tests`로 옮겨진 곳을 먼저 찾는다(#346이 test_runner.py를 test_run_*.py로 나눔).
- "작업 트리를 다시 만들었다"는 재시도: 먼저 `git show --stat HEAD`로 WIP가 합쳐졌는지 보고, 없으면 `origin/foragent/issue-<N>`의 이전 커밋을 `cherry-pick --no-commit` + `reset -q`로 가져온다. 재시작 때 미커밋 변경은 사라질 수 있다.
- 기준선 비교는 `git diff > .foragent/wip.patch; git apply -R`, 시험, `git apply`(`git checkout --`은 권한 거부).

## 시험과 기준선
- 전체 테스트(약 25분)는 실행 예산을 넘겨 작업 트리를 잃게 하므로 돌리지 않는다. 완료 기준과 바꾼 파일의 시험만 파일별로 `-rf`를 붙여 돌린다(여러 파일 한 호출은 출력이 빈 적이 있음). 인수 테스트는 수정하지 않는다. 샌드박스에서 uv 캐시를 못 쓰면 임시 `UV_CACHE_DIR`.
- 실제 kill 시험(`...real_runner_is_terminated...`)은 환경이 `os.killpg`를 막으면 `PermissionError`로 실패한다. 다시 돌리지 말고 `-k 'not real_runner_is_terminated'`로 빼고 REBUTTAL로 답한다.
- 남의 영역 시험이 "Acceptance baseline regression"이나 CI 실패로 지목되면: `gh run list --branch main --workflow ci`로 main도 깨지는지, 현재 HEAD에서 단독·파일 전체로 재현되는지 본다. 코드 변경 없는 커밋에서 깨졌다면 고정 `NOW`와 실제 시계가 섞인 시한폭탄이다(#769 test_cli inspect). 재현되지 않으면 코드를 건드리지 말고 REBUTTAL에 원인·file:line·반복 횟수를 적는다. 알려진 기준선 실패: `test_github_outbox.py::...rate_limited_close`(#705).
- 가짜 추적기에 가벼운 읽기를 더할 때 `self.list_issues()`를 재사용하지 말고 `self.issues`를 거른다. `test_tick_step_times.py`가 `list_issues` 호출을 tick당 한 번으로 센다.
