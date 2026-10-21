# Design lane 기억

- 색상 검사와 UI 의존성 감지는 `src/foragent/design.py`에 둔다. CLI는 설정·GitHub 연결 전에 `design.run`으로 분기하므로 일반 폴더에서도 `fa design lint`를 실행할 수 있다.
- UI 감지는 `order.context`의 기존 저장소 순회를 이용한다. 하위 프로젝트의 package.json도 읽고, 기존 제외 폴더(`node_modules`, `.git`, `.venv` 등)는 탐색하지 않는다. 컨텍스트 필드를 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 전체 키 검사도 갱신한다.
- 색상은 3·4·6·8자리 hex와 rgb(/hsl(를 검사한다. 중복 glob은 한 번만 검사하며, `--tokens`와 `--glob`을 주면 각 기본 패턴을 대체한다. UTF-8 오류는 대체 문자로 읽고 파일 읽기 OSError는 건너뛰어 보고서와 종료 코드를 유지한다.
- HTML 숫자 엔티티(`&#169;`, `&#8212;` 등)를 색상으로 오탐하지 않도록 hex 앞쪽 검사에서 `&`도 제외한다(`(?<![\w#&])`). 숫자만 있는 실제 3·4·6·8자리 색상은 계속 검출해야 하며, `tests/test_design.py::test_lint_ignores_html_character_references`가 정상 HTML의 종료 코드 0과 실제 색상 추가 시 종료 코드 1을 검증한다.
- 디자인 정책의 근거는 `docs/decisions.md` D33과 `docs/specs/2026-10-21-orchestrator-and-visibility.md`의 design-first 절이다. 디자인 기초를 기다리는 것은 시각 작업뿐이며, 탐색용으로 버릴 시제품은 예외다.
- 검증 명령: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py tests/test_order.py`, CI 전체 검증은 `uv run pytest -q --ignore=tests/acceptance`. 인수 테스트는 수정하지 않는다.
- 스킬 문서를 바꾸면 `tests/acceptance/test_fa_init_skill.py`와 `tests/acceptance/test_fa_order_skill.py`도 확인한다. fa-init의 기존 흐름은 survey → lanes add → baseline → CI → 최종 주문 안내 순서이며, 문서 검사는 각 명령의 첫 언급 순서도 확인한다.
