# Design lane 기억

## 구조
- 디자인 기초의 존재 기준은 `docs/design/CONTRACT.md` 파일이다. 제품 판단(D33)은 `skills/fa-order/SKILL.md`, 초기 감지·레인 제안은 `skills/fa-init/SKILL.md`에 둔다. `/fa-init`에서는 웹·TUI 화면의 디자인 방향을 정하는 스타일 하나만 `fa lanes skills <name> <style-id>`로 고정하고, tool/review/domain 스킬은 레인 에이전트가 이슈별로 고른다(D35). `ui.frameworks`가 비어 있어도 TUI나 계획된 화면이 있을 수 있으므로 코드·문서를 함께 읽는다.
- `fa design lint`는 저장소 설정이나 GitHub 없이 실행할 수 있도록 CLI의 설정·tracker 초기화 전에 분기한다. 기본 UI 패턴과 토큰 패턴, 프레임워크 감지는 `src/foragent/design.py`에 있다.
- 프로젝트 UI 탐색은 `os.walk`의 디렉터리 목록을 잘라 의존성·생성물·숨김 폴더를 방문하지 않는다. 비 UTF-8 파일은 대체 문자로 읽고 OSError 파일은 skipped 보고 후 계속한다.
- 색상 검사(`design.COLOR`)는 3·4·6·8자리 hex와 rgb(/hsl(를 잡는다. hex는 앞뒤 경계(`(?<![\w#])`, `(?![\w-])`)를 둔다. 경계가 없으면 `href="#add"`, `#face` 같은 URL 조각·ID 선택자가 색으로 잡혀 토큰만 쓰는 프로젝트도 lint가 실패한다(#41 리뷰).
- D72·D73의 디자인 모드는 `src/foragent/design_session.py`가 담당한다. `fa design preview`는 `--stage board --references manifest.json` 또는 반복 `--command`/`--url`을 받고, `picks`는 토픽별 JSON을 읽고 `done`은 저장한 미리보기 칸만 닫는다. 터미널 반환 핸들을 매번 저장해 일부 생성 실패 후에도 정리할 수 있게 한다.
- 레퍼런스 HTML·복사 이미지·투표는 `StateStore.root/design/`에만 저장한다. 토픽은 해시 경로로 나누고, 보드는 `127.0.0.1` HTTP 자식 프로세스가 서비스한다. gate의 run·lane 상태에는 기록하지 않는다. 로컬 서버 준비 파일을 기다린 뒤 URL을 열고 종료는 저장한 URL로 요청한다.
- `skills/fa-design-session/media/<medium>.md` 팩끼리 서로 가리킬 때는 같은 폴더 기준 상대 이름(`tui.md`)을 쓴다. 스킬은 다른 프로젝트에서 `.claude/skills/fa-design-session/`에 설치되므로 `skills/...` 저장소 경로는 존재하지 않는다(#178 리뷰).
- `fa orch request --from planning --text ...`는 별도 오케스트레이터 inbox 기능의 인계 인터페이스다.
- `fa order context` 최상위 키를 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 정확한 키 집합도 갱신한다. 외부 계정 없이 검증하려면 `order.context(root, Config(), FakeTracker())`를 쓴다.
- `cli.py` import 병합 때 `design`·`design_session`과 다른 레인 모듈(`watch` 등)을 모두 보존하고 전체 테스트로 확인한다.

## 검증
- 인수 테스트(`tests/acceptance/`)는 수정하지 않는다. 주요 명령:
  - 디자인 기초: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py`
  - `/fa-init` D35 안내·스타일 선택: `tests/acceptance/test_init_skill_cap.py`, `tests/acceptance/test_init_style_skills.py`. 기존 인수 테스트가 여섯 디자인 스킬 이름을 요구하므로 비스타일 스킬은 이슈별 선택 예시로 남긴다.
  - 디자인 모드·인터뷰: `fa test tests/acceptance/test_design_interview.py tests/acceptance/test_design_session.py tests/test_design_preview.py tests/test_design.py`
  - CI 전체: `uv run pytest -q --ignore=tests/acceptance`. 완료 전에는 `uv run pytest` 전체도 돌린다.
- 인수 테스트와 유닛 테스트의 파일명을 같게 만들면 pytest 기본 import 방식에서 충돌한다. 그래서 유닛 파일은 `test_design_preview.py`처럼 다른 이름을 쓴다.
- 스킬 문서 인수 테스트는 YAML 머리말(description)까지 포함한 전체 글을 읽는다. "A가 B보다 먼저" 순서 검사는 머리말의 낱말도 센다.
- 전체 테스트에는 다른 주문의 미구현 고정 인수 테스트가 섞여 있다(`invalid choice: 'brief'`, `implemented by another issue`, `next_parallel` import 실패 등). 새 회귀와 구분하려면 주문의 기존 통과 목록, 이슈의 Done when, 수정 전 HEAD의 같은 실패를 함께 본다.
- 전체 2천여 테스트는 직렬로 오래 걸린다. pytest-xdist를 임시 가상환경에만 넣고 `uv run pytest -n 6 --junitxml=/tmp/results.xml`로 돌린 뒤 XML 노드 ID로 기존 통과 목록을 대조한다. 저장소 의존성 파일은 바꾸지 않는다.
