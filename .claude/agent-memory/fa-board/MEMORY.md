# board 레인 현재 메모

- 보드는 이슈 라벨을 한 방향으로 보여 준다. 보드 값을 스케줄링이나 승인 판단에 사용하지 않는다. `board.sync_status(ctx, issue, status)`가 호출별 연결을 묶고 `board.set_status(issue, status)`의 모든 실패를 로그로 남겨 tick·runner를 계속 진행시킨다. 작업 간 연결을 전역 변수로 공유하지 않는다.
- Projects v2는 `Tracker.graphql(query, variables)`로만 접근한다. `GhTracker`는 `gh api graphql --input -`에 JSON을 전달하므로 중첩 입력·정수·null·줄바꿈을 그대로 보존한다. `FakeTracker`에서도 보드 생성·필드·항목·선택 값을 조회할 수 있다.
- 실제 GitHub는 새 프로젝트에 Status를 이미 만든다. 중복 생성 대신 조회 후 필요한 선택지를 맞춘다. 선택지를 갱신할 때 기존 `id`를 보내야 다른 항목의 선택 값이 지워지지 않는다. Lane은 기존 선택지를 보존하면서 새 레인을 추가한다.
- 보드 제목은 `Foragent: <저장소 이름>`, 소유자는 인증 사용자 대신 저장소 소유자다. 소유자의 보드와 필드 조회에는 페이지 처리가 필요하다. 설정에 id가 없어도 제목으로 기존 보드를 찾아 재사용한다.
- `fa init`의 보드 생성은 dry-run 및 기준 브랜치 검사 뒤에 한다. 필드 설정이 중간에 실패해도 생성한 id는 저장해 다음 init에서 같은 보드를 복구한다. 기존 YAML 주석과 변경 없는 파일의 수정 시각을 보존한다. `--no-board`는 보드 호출을 생략한다.
- 동일 주문 재발행에서 이미 승인된 이슈를 Approved로 다시 쓰면 Running/Ready가 되돌아간다. 승인 라벨이 새로 추가되는 전이에서만 Approved를 기록한다.
- CLI 옵션을 추가하면 `tests/test_cli.py`의 정확한 키워드 인자 기대값 두 곳을 함께 갱신한다. 설정 키는 `tests/test_config.py::test_exact_defaults`에도 추가한다. 기존 init 파일·복구 검증은 명시적인 `no_board=True` 경로로 유지하고 기본 보드 경로를 별도로 검증한다.
- #38 인수 시험: `uv run pytest -q tests/acceptance/test_board_projects.py`. CI 전체 시험: `uv run pytest -q --ignore=tests/acceptance`. acceptance 파일은 수정하지 않는다.
- 이 환경의 기본 uv 캐시는 쓰기가 제한된다. 준비된 캐시를 쓰려면 명령 앞에 `env UV_CACHE_DIR=/private/tmp/fa-issue-4-uv-cache`를 붙이고 `uv run --offline pytest ...`로 실행한다. 임시 Git 저장소에서 커밋·push는 기존 테스트 도우미가 모사한다.
