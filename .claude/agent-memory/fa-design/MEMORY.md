# Design lane 기억

- 디자인 기초의 존재 기준은 `docs/design/CONTRACT.md` 파일이다. 제품 판단(D33)은 `skills/fa-order/SKILL.md`, 초기 감지·레인 제안은 `skills/fa-init/SKILL.md`에 둔다. `/fa-init`에서는 웹·TUI 화면의 디자인 방향을 정하는 스타일 하나만 `fa lanes skills <name> <style-id>`로 고정하고, tool/review/domain 스킬은 레인 에이전트가 이슈별로 고른다. `ui.frameworks`가 비어 있어도 TUI나 계획된 화면이 있을 수 있으므로 코드·문서를 함께 읽는다.
- `fa design lint`는 저장소 설정이나 GitHub 없이 실행할 수 있도록 CLI의 설정·tracker 초기화 전에 분기한다. 기본 UI 패턴과 토큰 패턴, 프레임워크 감지는 `src/foragent/design.py`에 있다.
- 프로젝트 UI 탐색은 `os.walk`의 디렉터리 목록을 잘라 의존성·생성물·숨김 폴더를 방문하지 않는다. 색상값은 3·4·6·8자리 hex와 rgb(/hsl(를 검사한다. 비 UTF-8 파일은 대체 문자로 읽고 OSError 파일은 skipped 보고 후 계속한다.
- `fa order context` 최상위 키를 추가하면 `tests/test_order.py::test_context_all_sections_and_gate_groups`의 정확한 키 집합도 갱신한다. 외부 계정 없이 검증하려면 `order.context(root, Config(), FakeTracker())`를 쓴다.
- CLI import 병합에서 디자인 레인의 `design`과 상태 화면의 `watch`를 모두 보존한다. `GIT_SHA` 버전 표시와 `gate watch`·`orch watch` 분기도 유지하고 전체 테스트로 함께 검증한다.
- 검증: `uv run pytest -q tests/acceptance/test_design_first.py tests/test_design.py`, CI 전체: `uv run pytest -q --ignore=tests/acceptance`. 인수 테스트는 수정하지 않는다.
- `/fa-init`의 D35 안내 검증: `uv run pytest -q tests/acceptance/test_init_skill_cap.py`.
- `/fa-init` 스타일 선택 검증: `uv run pytest tests/acceptance/test_init_style_skills.py`. 기존 인수 테스트는 여섯 디자인 스킬 이름을 요구하므로 비스타일 스킬은 이슈별 선택 예시로 유지한다. 완료 전에는 `uv run pytest` 전체를 실행한다.
- D72·D73 보강 3의 디자인 모드는 `src/foragent/design_session.py`가 담당한다. `fa design preview`는 `--stage board --references manifest.json` 또는 반복 `--command`/`--url`을 받고, `picks`는 토픽별 JSON을 읽고 `done`은 저장한 미리보기 칸만 닫는다. 터미널 반환 핸들을 매번 저장해 일부 생성 실패 후에도 정리할 수 있게 한다.
- 레퍼런스 HTML·복사 이미지·투표는 `StateStore.root/design/`에만 저장한다. 토픽은 해시 경로로 나누고, 보드는 loopback HTTP 자식 프로세스가 서비스한다. gate의 run·lane 상태에는 기록하지 않는다. 로컬 서버 준비 파일을 기다린 뒤 URL을 열고 종료는 저장한 URL로 요청한다.
- 디자인 모드 검증: `uv run pytest tests/acceptance/test_design_session.py tests/test_design_preview.py tests/test_design.py`. 인수 테스트와 유닛 테스트의 파일명을 같게 만들면 pytest 기본 import 방식에서 충돌하므로 새 유닛 파일은 `test_design_preview.py`로 둔다. `fa orch request --from planning --text ...`는 별도 오케스트레이터 inbox 기능의 인계 인터페이스다.
- 이 저장소의 전체 테스트에는 아직 구현되지 않은 다른 주문의 고정 인수 테스트도 포함된다. 전체 실패 수와 새 회귀를 구분할 때는 주문에 제공된 기존 통과 목록, 해당 이슈의 Done when, 수정 전 HEAD 사본의 동일 실패를 함께 확인한다.
- 전체 2천여 테스트의 직렬 검증은 오래 걸린다. pytest-xdist를 임시 가상환경에만 추가한 `uv run pytest -n 6 --junitxml=/tmp/results.xml`로 전체를 검증하고, XML의 노드 ID로 기존 통과 목록 보존 여부를 대조할 수 있다. 저장소 의존성 파일은 바꿀 필요가 없다.
- D73 인터뷰와 매체 리서치는 `skills/fa-design-session/SKILL.md`와 `media/{web,mobile,tui,pixel,cli}.md`가 담당한다. 터미널 UI의 파일 키는 `tui`이며 공용 교훈도 `~/.local/share/foragent/media/tui.md`로 읽는다. 혼합 매체는 팩을 함께 읽고, 참고 이미지는 외부 로컬 상태에만 두며 `docs/design/<topic>/HANDOFF.md`에는 링크·메모·원칙을 남긴다. 검증: `uv run pytest tests/acceptance/test_design_interview.py tests/acceptance/test_design_session.py tests/test_design_preview.py tests/test_design.py`.
- 제한된 실행 환경에서 uv 기본 캐시에 쓰지 못하면 `UV_CACHE_DIR=/tmp/foragent-issue-178-uv-cache`처럼 쓰기 가능한 경로를 지정한다. `uv run pytest`가 실행 파일 없음으로 실패해도 `.venv/bin/python -m pytest --version`이 성공하면 패키지가 아니라 `.venv/bin/pytest` 진입점 누락이다. `uv sync`의 패키지 검사만으로는 복구되지 않을 수 있으므로 진입점을 복구한 뒤 원래 검증 명령을 다시 실행한다.
- `skills.py`는 스킬 폴더 전체를 대상 프로젝트의 `.claude/skills/<name>/`에 복사한다. 리서치 팩 같은 동봉 자료는 원본 저장소 경로를 안내하더라도 실행할 때는 읽은 `SKILL.md` 옆의 상대 경로로 찾도록 명시한다. 팩에서 다른 팩을 열 때도 현재 파일 기준의 상대 링크(예: `pixel.md` → `tui.md`)를 써야 설치된 프로젝트에서 찾을 수 있다.
