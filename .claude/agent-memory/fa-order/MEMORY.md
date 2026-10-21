# Order lane 기억

- 설정 필드를 추가할 때 `Config`에 기본값을 넣으면 `DEFAULTS = asdict(Config())`와 알 수 없는 키 검사에 반영된다. 타입·허용값 검증은 `config._validate`에 둔다. 로컬·전역 설정은 같은 검증을 사용한다.
- 설정 기본값과 주문 응답의 전체 항목을 검사하는 테스트가 있다. 필드 추가 시 `tests/test_config.py::test_exact_defaults`, `tests/test_order.py::test_context_all_sections_and_gate_groups`도 갱신한다.
- 인터뷰 설치 정보는 현재 `Path.home() / ".claude/plugins/installed_plugins.json"`에서 읽는다. `plugins`의 `ouroboros@`로 시작하는 키에서 첫 항목 버전을 사용한다. 파일·구조가 손상됐으면 미설치로 처리한다. 버전 고정은 문자열 완전 일치이며 `None`만 고정 없음이다.
- 외부 계정 없이 주문 컨텍스트를 검증하려면 `order.context(root, config, FakeTracker())`를 사용한다. 설치 파일 테스트는 임시 HOME으로 격리한다.
- 검증: `uv run pytest -q tests/acceptance/test_interview_engine.py tests/test_order.py tests/test_config.py`. 인수 테스트는 수정하지 않는다.
