# Order lane 프로젝트 기억

- 설정 원본은 `src/foragent/config.py`의 `Config`이며, `DEFAULTS = asdict(Config())`가 허용 키를 결정한다. 새 설정은 필드와 `_validate`를 함께 추가하고, `tests/test_config.py::test_exact_defaults`도 갱신한다.
- `order.context`의 최상위 항목을 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 항목 집합도 갱신한다.
- 인터뷰 엔진은 `$HOME/.claude/plugins/installed_plugins.json`의 `plugins` 매핑에서 `ouroboros@`로 시작하는 키의 첫 항목 버전을 확인한다. 다른 마켓플레이스 이름도 허용하며, 두 번째 항목 버전으로 대체하지 않는다. 파일 누락·읽기 오류·JSON/구조 손상은 미설치로 처리한다.
- `interview`는 기본 `ouroboros`, `ouroboros_version`은 기본 `null`이다. 버전 지정은 문자열 동등 비교이며, native를 명시하면 설치 상태나 버전과 관계없이 native를 선택한다. 전환이 없으면 `reason`은 `null`이다.
- 관련 검증: `uv run pytest -q tests/acceptance/test_interview_engine.py tests/test_config.py tests/test_order.py`. 수용 테스트는 수정하지 않는다.
- 제한된 작업 환경에서 uv 기본 캐시 접근이 거부되면 `UV_CACHE_DIR=/tmp/foragent-issue-7-uv-cache`를 명령 앞에 붙여 실행한다.
