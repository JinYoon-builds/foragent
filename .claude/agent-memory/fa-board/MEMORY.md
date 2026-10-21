# Board lane 기억

- 보드 명세는 `docs/specs/2026-10-21-orchestrator-and-visibility.md`의 `board-projects` 및 `docs/decisions.md` D31이다. 보드는 이슈 라벨을 표시하는 단방향 화면이며 장애가 발행·tick·run을 실패시키면 안 된다.
- `Tracker.graphql(query, variables)`는 GitHub 형태의 `{"data": ...}`를 반환한다. `GhTracker`는 중첩 입력·숫자·null을 보존하도록 `gh api graphql --input -`의 JSON 표준 입력을 쓴다. HTTP 성공이어도 GraphQL `errors`를 확인한다. `FakeTracker`는 같은 작업과 스냅샷을 오프라인으로 제공한다.
- Projects v2는 기본 `Status` 필드를 제공한다. 기존 필드를 찾아 선택지를 갱신하고, 보드 검색과 필드 조회는 페이지를 끝까지 읽는다. 보드 제목은 `Foragent: <repo>`이고 소유자는 인증 사용자 대신 저장소 소유자다.
- `updateProjectV2Field.singleSelectOptions`는 전체 선택지를 교체한다. 기존 선택지의 `id`, `name`, `color`, `description`을 전달해야 항목 값을 보존한다. 공식 문서: https://docs.github.com/en/graphql/reference/projects#projectv2singleselectfieldoptioninput
- `board.sync_status(ctx, issue, status)` 또는 발행용 `board.update_status(config, tracker, repo, issue, status)`가 저장소별 연결을 한 호출 동안 바인딩한 뒤 두 인수의 `board.set_status(issue, status)`를 호출하고 오류를 기록한다. 전역 저장소 연결을 두지 않는다. 같은 이슈를 다시 추가하면 기존 항목을 재사용하며, 반복 승인으로 Running 상태를 Approved로 되돌리지 않는다.
- 새 레인을 발행한 직후에는 전달받은 Config가 이전 레인 목록일 수 있다. 보드의 Lane 값은 생성된 이슈의 `lane:` 라벨을 읽어 새 선택지를 추가한다.
- Runner의 Running 갱신은 승인 스냅샷을 확인한 뒤에 한다. 승인이 없어 실행을 거절하는 경로가 이미 표시된 Needs human 상태를 덮어쓰지 않아야 한다.
- 중단된 작업은 `actions.flag`가 `blocked` 라벨·댓글과 보드 `Blocked`로 표시한다. scheduler는 `needs-human` 라벨이 있을 때만 `Needs human`으로 표시한다. `orchestrator_timeout_minutes`를 넘긴 뒤 두 라벨이 공존하면 `Needs human`을 우선한다. 관련 회귀 검증: `uv run pytest -q tests/test_orch_blocked.py -k board_tracks`.
- 초기화는 보드 id를 `.foragent.yml`에 저장하고 `--no-board`·`--dry-run`에서는 보드 호출을 하지 않는다. 기존 YAML에 `board: null`이 있으면 중복 키를 덧붙이지 않고 교체한다.
- 검증: `uv run pytest -q tests/acceptance/test_board_projects.py`; 관련 회귀 테스트는 `tests/test_board.py`, `tests/test_init.py`, `tests/test_order.py`, `tests/test_config.py`, `tests/test_tracker.py`, `tests/test_scheduler.py`, `tests/test_runner.py`다. 인수 테스트는 수정하지 않는다.
- 일반 테스트와 인수 테스트에 `test_orch_blocked.py`처럼 같은 이름이 있다. 같은 pytest 호출에 섞으면 모듈 이름 충돌로 수집이 실패하므로 각각 실행한다. CI도 일반 테스트는 `uv run pytest -q --ignore=tests/acceptance`로 분리한다.
- 제한된 환경에서 uv 캐시 쓰기가 막히면 `UV_CACHE_DIR=/private/tmp/foragent-issue-38-uv-cache`를 사용한다. 패키지 서버 DNS가 막히면 기존 `~/.cache/uv/wheels-v6/pypi/<package>`의 잠금 버전 메타데이터와 archive 대상을 쓰기 가능한 캐시로 복사해 재사용한다. PyYAML wheel은 실행 Python 버전과 플랫폼이 맞아야 한다.
