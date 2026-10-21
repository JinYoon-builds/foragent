# Design lane 기억

- 디자인 검사는 `src/foragent/design.py`에 둔다. CLI 등록·분기는 `src/foragent/cli.py`, 주문 연결은 `order.context()` 응답의 `ui` 필드다. `design` 분기는 Git 설정·GitHub tracker를 만들기 전에 실행해야 일반 UI 폴더에서도 오프라인 완료 검사로 쓸 수 있다.
- `fa design lint`의 `--tokens`와 `--glob`는 반복 지정과 한 번에 여러 값 지정을 모두 지원한다. 사용자 지정값은 각 기본 패턴을 대체한다. 토큰·대상 경로를 실제 경로로 비교해 겹치는 glob과 토큰 심볼릭 링크가 중복 보고되지 않도록 한다. 결과는 `violations` 배열에 파일·행·열·색상 리터럴을 담고, 위반이 있으면 종료 코드 1이다.
- UI 감지는 하위 패키지의 dependencies·devDependencies도 읽고 HTML 파일을 감지한다. `.git`, `.venv`, `node_modules` 등은 탐색에서 제외한다. 디자인 기초는 `docs/design/CONTRACT.md` 파일 유무로만 판단한다. 응답 필드 추가 시 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 전체 키 검사도 갱신한다.
- D33에서 디자인 기초를 기다리는 것은 시각 작업뿐이다. API·로직·스타일 없는 기능 뼈대와 E2E는 병렬 진행한다. init은 UI 감지·디자인 레인 제안, order는 취향 인터뷰·계약서·기초 이슈를 담당한다.
- 검증: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py tests/test_order.py tests/test_cli.py`. `tests/acceptance/`는 수정하지 않는다.
- 제한 환경에서는 `UV_CACHE_DIR`을 쓰기 가능한 임시 경로로 지정한다. 다운로드가 막히면 기존 uv 캐시의 필요한 `wheels-v6/pypi/<package>` 메타데이터와 `archive-v0/<id>`를 임시 캐시에 함께 연결해 `uv run --offline`으로 검증할 수 있다. wheels 메타데이터만 옮기면 archive id를 찾지 못한다.
