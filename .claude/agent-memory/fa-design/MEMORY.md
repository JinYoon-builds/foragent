# Design lane 기억

- 디자인 기초의 존재 기준은 `docs/design/CONTRACT.md` 파일이다. 제품 판단(D33)은 `skills/fa-order/SKILL.md`, 초기 감지·레인 제안은 `skills/fa-init/SKILL.md`에 둔다. `/fa-init`에서는 웹·TUI 화면의 디자인 방향을 정하는 스타일 하나만 `fa lanes skills <name> <style-id>`로 고정하고, tool/review/domain 스킬은 레인 에이전트가 이슈별로 고른다. `ui.frameworks`가 비어 있어도 TUI나 계획된 화면이 있을 수 있으므로 코드·문서를 함께 읽는다.
- `fa design lint`는 저장소 설정이나 GitHub 없이 실행할 수 있도록 CLI의 설정·tracker 초기화 전에 분기한다. 기본 UI 패턴과 토큰 패턴, 프레임워크 감지는 `src/foragent/design.py`에 있다.
- 프로젝트 UI 탐색은 `os.walk`의 디렉터리 목록을 잘라 의존성·생성물·숨김 폴더를 방문하지 않는다. 색상값은 3·4·6·8자리 hex와 rgb(/hsl(를 검사한다. 비 UTF-8 파일은 대체 문자로 읽고 OSError 파일은 skipped 보고 후 계속한다.
- `fa order context` 최상위 키를 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 정확한 키 집합도 갱신한다. 외부 계정 없이 검증하려면 `order.context(root, Config(), FakeTracker())`를 쓴다.
- CLI import 병합에서 디자인 레인의 `design`과 상태 화면의 `watch`를 모두 보존한다. `GIT_SHA` 버전 표시와 `gate watch`·`orch watch` 분기도 유지하고 전체 테스트로 함께 검증한다.
- 검증: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py`, CI 전체: `uv run pytest -q --ignore=tests/acceptance`. 인수 테스트는 수정하지 않는다.
- `/fa-init`의 D35 안내 검증: `uv run pytest -q tests/acceptance/test_init_skill_cap.py`.
- `/fa-init` 스타일 선택 검증: `uv run pytest tests/acceptance/test_init_style_skills.py`. 기존 인수 테스트는 여섯 디자인 스킬 이름을 요구하므로 비스타일 스킬은 이슈별 선택 예시로 유지한다. 완료 전에는 `uv run pytest` 전체를 실행한다.
- D72·D73 보강 3: 디자인은 기획 세션 안의 모드다. `design_session.py`는 미리보기 칸·서버 수명, `design_board.py`는 로컬 HTML·HTTP·선택 기록을 담당한다. `design-preview.json`과 `design/<주제 해시>/picks.json`은 StateStore 아래에만 두며 gate 실행 기록에 넣지 않는다. 종료에는 저장한 미리보기 핸들만 사용하고 `watch-terminal.json`의 레인 화면은 보존한다.
- argparse에서 최상위 명령의 `dest="command"`를 미리보기 `--command`가 덮어쓰지 않도록 `dest="preview_commands"`를 쓴다. 인수 테스트와 단위 테스트의 파일 이름도 겹치면 수집 오류가 나므로 회귀 검사는 `tests/test_design_preview.py`에 둔다.
- 디자인 모드 잠금과 투표 잠금은 분리한다. 미리보기 종료가 서버 응답을 기다리는 동안 진행 중인 투표가 같은 잠금을 기다리면 멈출 수 있다. 브라우저 보드는 loopback 서버에서 로컬 썸네일만 제공하고, 웹 원본은 Playwright CLI 캡처 또는 미리 준비한 로컬 이미지로 받는다.
- #164 검증: `uv run pytest tests/acceptance/test_design_session.py`, 회귀 검사 `uv run pytest -q tests/test_design_preview.py tests/test_design.py`. 샌드박스에서 기본 uv 캐시를 못 쓰면 `UV_CACHE_DIR=/tmp/foragent-uv-cache`를 쓰고, 네트워크가 없으면 이미 설치된 의존성을 작업 폴더의 가상환경으로 복사해 실행할 수 있다.
- `--stage drafts`는 2~3개 비교용이다. 선택한 시안 하나를 다듬을 때는 `--stage` 없이 `--command`나 `--url`을 쓴다. 웹 시안 서버는 기획 에이전트가 관리하고, 미리보기 도구는 자체 비교 페이지 서버만 종료한다.
- 전체 테스트에는 다른 이슈의 미구현 인수 검사도 포함된다. 실패가 있으면 수정 전 `HEAD`의 테스트별 결과와 대조해 기존 통과 검사가 유지되는지 확인하고 `.foragent/VALIDATION.md`에 남긴다. `fa orch request` 구현은 오케스트레이터 레인의 연결점이며 디자인 레인에서 중복 구현하지 않는다.
