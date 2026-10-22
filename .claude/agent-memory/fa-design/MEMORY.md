# Design lane 기억

- 디자인 기초의 존재 기준은 `docs/design/CONTRACT.md` 파일이다. 제품 판단(D33)은 `skills/fa-order/SKILL.md`, 초기 감지·레인 제안은 `skills/fa-init/SKILL.md`에 둔다. `/fa-init`에서는 웹·TUI 화면의 디자인 방향을 정하는 스타일 하나만 `fa lanes skills <name> <style-id>`로 고정하고, tool/review/domain 스킬은 레인 에이전트가 이슈별로 고른다. `ui.frameworks`가 비어 있어도 TUI나 계획된 화면이 있을 수 있으므로 코드·문서를 함께 읽는다.
- `fa design lint`는 저장소 설정이나 GitHub 없이 실행할 수 있도록 CLI의 설정·tracker 초기화 전에 분기한다. 기본 UI 패턴과 토큰 패턴, 프레임워크 감지는 `src/foragent/design.py`에 있다.
- 프로젝트 UI 탐색은 `os.walk`의 디렉터리 목록을 잘라 의존성·생성물·숨김 폴더를 방문하지 않는다. 색상값은 3·4·6·8자리 hex와 rgb(/hsl(를 검사한다. 비 UTF-8 파일은 대체 문자로 읽고 OSError 파일은 skipped 보고 후 계속한다.
- `fa order context` 최상위 키를 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 정확한 키 집합도 갱신한다. 외부 계정 없이 검증하려면 `order.context(root, Config(), FakeTracker())`를 쓴다.
- CLI import 병합에서 디자인 레인의 `design`과 상태 화면의 `watch`를 모두 보존한다. `GIT_SHA` 버전 표시와 `gate watch`·`orch watch` 분기도 유지하고 전체 테스트로 함께 검증한다.
- 검증: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py`, CI 전체: `uv run pytest -q --ignore=tests/acceptance`. 인수 테스트는 수정하지 않는다.
- `/fa-init`의 D35 안내 검증: `uv run pytest -q tests/acceptance/test_init_skill_cap.py`.
- `/fa-init` 스타일 선택 검증: `uv run pytest tests/acceptance/test_init_style_skills.py`. 기존 인수 테스트는 여섯 디자인 스킬 이름을 요구하므로 비스타일 스킬은 이슈별 선택 예시로 유지한다. 완료 전에는 `uv run pytest` 전체를 실행한다.
