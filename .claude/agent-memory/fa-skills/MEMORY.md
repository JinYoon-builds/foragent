# skills 레인 현재 메모

## 스킬 설치와 작업공간 복사
- 라이브러리 스킬 설치 기본값은 로컬 복사다(D37). `.gitignore`에는 `/.claude/skills/<이름>/` 규칙만 한 번 덧붙이고 기존 내용은 바이트 그대로 둔다. `.claude/skills/` 전체를 빼면 사용자 스킬도 빠지므로 개별 경로를 쓴다.
- 라이선스 차단과 `THIRD_PARTY_NOTICES.md` 생성은 `install(..., commit=True)` / `fa skills install --commit`에서만 한다. 로컬 설치는 불명확·비공개 라이선스도 경고 후 허용하고 Git 조회가 필요 없다.
- `lane_files()`는 레인 설정·에이전트 정의·`.gitignore`만 반환한다. `order.publish()` 중단 복구도 이 함수를 쓰므로 스킬 파일을 넣으면 강제 추적·커밋된다.
- runner는 작업공간을 만들거나 다시 만들 때(대상 브랜치 변경·패치 충돌 복구 포함) 스킬을 복사한다. 순서는 저장소 `skills/<이름>/` 제품 스킬 → `.claude/skills` 로컬 설치본 → `available_only=True` 캐시다. 기존 작업공간의 스킬은 보존한다.
- 복사한 경로만 `GitRepo.local_skill_paths`에 등록한다. 자동 스테이징에서 빼고, 실행기가 강제로 스테이징한 복사본도 커밋 전에 인덱스에서 지운다. 사용자 스킬은 계속 커밋된다.

## 라이브러리·카탈로그
- 캐시는 `~/.local/share/foragent/libraries/<이름>`이다. `SkillManager.skills()`는 등록됐는데 디스크에 없는 라이브러리를 복제한다(D58). 기존 디렉터리는 갱신하지 않는다. `available_only=True`는 누락 라이브러리를 건너뛰어 복제 없이 오프라인으로 돈다. 실제 캐시 확인은 이것으로 한다. 복제 실패는 라이브러리 이름이 든 `SkillError`다.
- `_skill_files()`는 os.walk로 `**/skills/<이름>/SKILL.md`를 찾는다(#730). 숨김 폴더·node_modules는 건너뛰되 자식이 `skills`인 숨김 도구 폴더는 들어간다. 옛 glob처럼 디렉터리 심볼릭 링크를 따라가고, 자기 조상 실경로로 돌아가는 링크만 끊는다.
- 도구별 사본(`skills/<이름>`, `.<도구>/skills/<이름>`)은 skills 앞 경로(마지막 숨김 폴더 제외)+이름으로 묶는다. 공용 사본을 대표로 고르고 `copies`에 모든 상대 경로를 남긴다. ID는 옛 규칙(`이름` 또는 `첫폴더/이름`, 예: `engineering/skills/<이름>`)이고 충돌할 때만 대표 사본 폴더 경로를 쓴다. 완전한 ID는 이름 별칭보다 우선한다.
- 라이선스: 라이브러리는 `_license(..., nearest=True)`로 가장 가까운 LICENSE를 보고 덧붙인 글을 허용한다. 앞부분 표준 본문이 SPDX보다 먼저 판정한다(덧붙인 SPDX가 뒤집지 못함, #730 리뷰). 저장소 제품 스킬은 옛 규칙이다.
- 저장소 `skills/<이름>/` 제품 스킬은 이름으로 라이브러리보다 먼저 해석된다(`check_install`이 `_repo_skill()`을 먼저 봄). 완전한 라이브러리 ID는 계속 라이브러리 사본을 고른다.
- 스킬을 반입할 때 LICENSE 첫 줄에 `SPDX-License-Identifier: MIT`를 둔다. 머리말을 덧붙이면 표준 MIT 판별이 깨진다. knowledge-work-plugins 루트 LICENSE는 "limitations under the License."까지만 복사한다. 그 뒤는 무관한 글이다(#727).
- 카탈로그 `role`은 미확정이면 `null`이다. `runs_scripts`는 파일 경로만 보고 모든 사본을 합산한다. 스크립트를 읽거나 실행하지 않는다.
- `_scan()`은 (유효 스킬, 건너뜀 `{path, error}`)를 돌려준다. 깨진 SKILL.md는 catalog·install에서 조용히 빠지고 `add`는 `skipped`로 알린다. 유효 0개+건너뜀 있음일 때만 모든 경로를 담아 실패한다(#232).
- `fa skills add SOURCE [--json]`은 임시 복제본만 읽고 지운다. `--approve --role SKILL=style|review|tool|domain`으로 모든 스킬의 역할을 줘야 전역 `skill_libraries`·`skill_roles`를 저장한다. 실제 캐시는 이후 자동 복제가 만든다.

## 레인 스킬
- 레인 스킬의 원본은 `.foragent.yml` 하나다(D95, #725). `lane_skill_names()`와 `order.context()` 모두 에이전트 frontmatter를 대체로 읽지 않는다. #725는 읽는 곳 하나에 대체 읽기를 남겨 반려됐다. 고칠 땐 `grep -rn '"skills"\|\.claude/agents' src/foragent`로 모든 읽는 곳을 함께 본다. 시험 fixture도 lane 설정에 `skills`를 넣어야 한다. `fa doctor --live`는 `runner_doctor.lane_skill_drift()`로 어긋난 레인만 알리고 1을 반환한다.
- `fa lanes skills <레인> [ids]`는 고정 목록을 통째로 교체한다. ID 생략은 비우기다. 붙일 땐 기존 목록을 모두 다시 적는다. 이 명령은 `.foragent.yml`·에이전트 frontmatter·`.gitignore` 줄·`.claude/skills/` 로컬 사본을 함께 만든다. Files 목록에 `.gitignore`가 없어도 정상 결과다. 에이전트 본문·다른 필드는 보존하고 설치본은 지우지 않는다. 실행 전 `SkillManager(load_config(root)).skills(available_only=True)`로 캐시를 확인하면 홈으로의 복제를 피한다. LICENSE 없는 제품 스킬은 "installing locally only" 경고 뒤 성공한다.
- 레인 생성·스킬 변경은 `_check_skills()`가 설치 전에 모든 ID·설치 디렉터리 중복·`style` 최대 하나를 확인한다. 역할은 해석된 `Skill.role`로 판정한다. 실패하면 레인 파일과 설치 디렉터리가 그대로여야 한다.
- 레인 에이전트 파일은 없을 수 있다. `set_lane_skills()`는 설치 전에 `read_text()`의 `OSError`를 경로가 든 `LaneError`로 바꾼다(#102 리뷰).
- 설정 `_overlay()`는 `tiers`만 깊게 병합하고 `lanes`는 전역 → 로컬 순으로 목록 전체를 교체한다. 처음 로컬에 저장할 때 상속한 다른 레인은 보존하되 전역 전용 레인을 복사하지 않는다(회귀: `test_set_lane_skills_does_not_copy_overridden_global_lanes`).
- [.foragent.yml 줄 단위 편집](settings_roundtrip.md) — lanes add/skills는 compose 위치로 줄만 고치고 다시 읽어 확인한다. 드문 YAML 형태는 쫓지 말고 거부한다. ruamel 왕복은 거절됨(#233).

## 리뷰·게이트
- `review_scope` 기본값은 `claude-local`이다(D39). `pr-body`는 리뷰마다 검증한 변경을 커밋·푸시해 같은 PR을 갱신하고 `/code-review <PR 번호> <review_level>`을 부른다. 임시 `CLAUDE.local.md`는 만들지 않는다. PR 본문은 생성·재리뷰·공개 모두 같은 렌더러다. `pr-body`는 범위 규칙과 판정만 본문에, 라운드 지적은 PR 댓글에 둔다.
- 기본 리뷰 관점은 `review_perspectives.py`의 `DEFAULT_PERSPECTIVES` 끝에 붙인다(#628 docs, #729 performance). focus 문구는 이슈 그대로 쓴다. 회귀: `test_review_perspectives_unit`, `test_config`, `test_orch_reviewers`, `test_review_conflict_paths`.
- 리뷰어는 브랜치를 최신 main과 비교한다. main이 앞서가면 "시험 삭제"로 보인다. merge-base 비교로 확인하고 REBUTTAL에 쓴다(#233).
- 리뷰 지적이 "broader class"를 말하면 같은 종류를 전부 찾는다. #725·#730은 지적 하나만 고쳤다면 다시 반려됐을 사례다.
- `PublicationError` 문구는 `messages/conflicts.py`의 `conflict_marker_left`로 렌더한다(#845). 언어는 오류 때 `GitRepo._language()`가 `load_config(root)`로 읽고 실패하면 `en`이다. runner fixture는 영어 문구를 본다.
- 오케스트레이터 시간 초과(`scheduler_checks._escalate_overdue_blocker`)는 최신 blocked 댓글과 `store.finish_times()`(키 `(번호, started_at)`)의 최신 값 중 늦은 쪽부터 센다(#642). 종료 시각을 바꾸려면 같은 state 경로로 `now`가 다른 `StateStore`를 만들어 `finish_run`한다.

## 시험 환경
- HOME·XDG·GIT_CONFIG_GLOBAL 격리는 conftest의 `pytest_runtest_setup`(tryfirst) 훅이 한다(#655). autouse fixture로는 순서가 보장되지 않는다. 복원은 `pytest_runtest_teardown` hookwrapper의 yield 뒤다. 확인은 `pytest_fixture_setup`에서 HOME을 기록하는 `-p` 임시 플러그인으로 한다(`--setup-plan`에는 훅이 안 보임).
- [게이트 슬롯 환경 변수](gate_slot_env.md) — FORAGENT_TEST_SLOT=held가 시험에 새면 capacity 슬롯 시험이 게이트에서만 실패한다.
- `src/foragent/commands/*.py`의 최상위 import는 `facade`·`repo_store`만 허용된다(`tests/test_cli_commands.py::test_command_modules_import_nothing_that_cli_already_binds`). 다른 이름은 함수 안 import나 모듈 `__getattr__`로 낸다. 명령 파일을 쪼갤 땐 이 시험을 같이 돌린다(#662 CI 실패).
- 넓은 acceptance 묶음에는 미구현 주문의 시험이 원래 실패한다(#100 재시도 소진 때도 capacity_aimd·quota_mode 등이 섞였음). 회귀 판단은 단위 시험이나 변경 전후 비교로 한다.
- `fa test`는 슬롯 대기로 10분을 넘길 수 있으니 백그라운드로 돌린다. 맞는 파일이 없는 glob을 넘기면 zsh가 인자 없이 실행해 전체 묶음이 돈다. 파일 이름은 먼저 `ls`로 확인한다.
- 등록 회귀는 `tests/test_skill_registration.py`에 둔다. acceptance와 같은 파일 이름(`test_skills_add.py`)을 쓰면 pytest import 충돌이 난다. acceptance 파일은 고치지 않는다.
- uv 캐시: 기본 캐시에 쓰기가 막히면 `/private/tmp`에 작업 전용 캐시를 복사해 `UV_CACHE_DIR=... uv run --offline ...`으로 돌린다. "저장소 밖 무변경" 조건이 있으면 첫 `uv run`이 `~/.cache/uv`에 남긴 editable 빌드(`archive-v0/<id>`, `sdists-v9/editable/<hash>`) 중 `.pth`가 이 작업 트리 `src`를 가리키는 두 항목만 지운다. 전후 비교는 `find -newer 표식 파일`로 한다(#727).

## 검증 명령
- 로컬 설치 `tests/acceptance/test_skills_local.py` · 리뷰 모드 `test_skills_review_scope.py`(회귀 `tests/test_runner.py -k pr_body_reviews`) · 자동 복제 `test_skills_autosync.py` · 등록 `test_skills_add.py` · 부분 팩 `test_skills_add_partial_pack.py` · 고정 스킬 `test_lane_fixed_skills.py` · fetch 경합 `test_fetch_race.py` · 문서 역할 `test_docs_role.py`.
- 단위 회귀: `tests/test_skills_lanes.py`(`fixed_lane`, `settings_document`, 자동 복제는 로컬 bare 저장소 사용), `tests/test_git.py -k fetch`, `tests/test_runner.py`, `tests/test_cli.py`, `tests/test_order.py`, `tests/test_config.py`, `tests/test_executors.py`.

## Git
- `GitRepo.fetch()`는 공유 clone의 참조 경합(`incorrect old value provided`)만 짧게 기다려 최대 3회 시도한다(#134). `_call()` 전체에 재시도를 넣지 않는다. 다른 오류는 즉시 원문으로 낸다.
