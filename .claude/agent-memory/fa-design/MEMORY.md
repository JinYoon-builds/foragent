# Design lane 기억

- 색상 검사는 `src/foragent/design.py`에 둔다. `fa design lint`는 git·설정·GitHub 연결 없이 현재 폴더에서 실행되도록 `cli.main`의 설정 로딩 전에 분기한다. 위반 시 1, 정상·매칭 없음은 0이며 JSON은 `violations` 배열에 파일·줄·열·색상 리터럴을 담는다.
- Python `Path.glob('**/*.css')`는 루트 파일도 포함한다. 여러 패턴이 같은 파일을 고르면 중복을 제거하고 정렬한다. 토큰 제외 비교는 resolve한 경로로 하며, 명시한 `--tokens`·`--glob`은 기본 패턴을 대체한다. 반복 옵션과 한 옵션 뒤 여러 값 모두 지원한다.
- UI 감지는 `order.context`의 기존 폴더 순회를 재사용한다. 중첩 package.json의 dependencies·devDependencies와 HTML을 읽고, node_modules·.venv 등 기존 제외 경로를 유지한다. `docs/design/CONTRACT.md` 파일이 디자인 기초의 표지다.
- 주문 컨텍스트 필드를 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 전체 키 검사도 갱신한다. 수용 테스트 `tests/acceptance/`는 수정하지 않는다. 외부 연결 없이 `context(root, Config(), FakeTracker())`로 확인할 수 있다.
- D33: UI 작업이고 기초가 없으면 디자인 기초 이슈를 먼저 만든다. 시각 작업만 기다린다. API·로직·스타일 없는 화면 구조와 E2E는 병렬 진행한다. 버릴 탐색 시제품은 예외이며, 취향은 참고 앱 2–3개에서 정한다.
- 검증: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py tests/test_order.py tests/test_cli.py`.
- 제한된 실행 환경에서 uv 기본 캐시 쓰기가 거부되면 `UV_CACHE_DIR=/private/tmp/foragent-issue-41-uv-cache`를 사용한다. 네트워크가 차단되어도 기존 `/Users/apple/Dev/foragent/.venv`에 잠금 파일과 동일한 테스트 의존성이 있으면 현재 작업 폴더의 가상환경에 복사해 검증할 수 있다. 원본 가상환경은 수정하지 않는다.
