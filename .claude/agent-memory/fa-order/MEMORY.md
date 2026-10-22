# Order lane 기억

- 설정 필드를 추가할 때 `Config`에 기본값을 넣으면 `DEFAULTS = asdict(Config())`와 알 수 없는 키 검사에 반영된다. 타입·허용값 검증은 `config._validate`에 둔다. 로컬·전역 설정은 같은 검증을 사용한다.
- 설정 기본값과 주문 응답의 전체 항목을 검사하는 테스트가 있다. 필드 추가 시 `tests/test_config.py::test_exact_defaults`, `tests/test_order.py::test_context_all_sections_and_gate_groups`도 갱신한다.
- 인터뷰 설치 정보는 현재 `Path.home() / ".claude/plugins/installed_plugins.json"`에서 읽는다. `plugins`의 `ouroboros@`로 시작하는 키에서 첫 항목 버전을 사용한다. 파일·구조가 손상됐으면 미설치로 처리한다. 버전 고정은 문자열 완전 일치이며 `None`만 고정 없음이다.
- 외부 계정 없이 주문 컨텍스트를 검증하려면 `order.context(root, config, FakeTracker())`를 사용한다. 설치 파일 테스트는 임시 HOME으로 격리한다.
- 검증: `uv run pytest -q tests/acceptance/test_interview_engine.py tests/test_order.py tests/test_config.py`. 인수 테스트는 수정하지 않는다.
- 주문 스킬의 인터뷰 분기는 설정 요청값이 아니라 `fa order context --json`의 `interview.engine`을 따른다. `interview.reason`은 native 전환 설명에 활용하며, seed도 실패하면 기존 답변을 유지한 채 AskUserQuestion으로 이어간다. seed는 3단계 되짚기와 4단계 분할의 확정 맥락이다.
- 스킬 문서 변경 검증: `uv run pytest -q tests/acceptance/test_fa_order_skill.py`. 이 테스트는 문서를 직접 읽으며 외부 계정이 필요 없다.
- #109: 인터뷰 기본값은 `native`. Ouroboros 폴백 테스트는 `Config(interview="ouroboros")`를 명시해야 한다. `profile`은 매핑 또는 `None`이며 주문 컨텍스트 최상위에 그대로 나온다.
- `fa profile set --kind ... --stage ... --priority ...`은 Git/GitHub 초기화 전에 처리한다. 저장은 `config.set_profile`에서 로컬 YAML만 갱신해 전역 설정을 복사하지 않고 `date.today().isoformat()`을 기록한다.
- 제한 환경에서 기본 uv 캐시를 쓸 수 없으면 `UV_CACHE_DIR`를 `/tmp`로 지정한다. 네트워크도 막혔으면 기존 `wheels-v6`, `simple-v21` 및 필요한 패키지의 `archive-v0` 디렉터리를 임시 캐시에 복사하고 `UV_OFFLINE=true uv run pytest`로 실행할 수 있다.
- #110: `validate_plan`은 신규 `issues`와 기존 이슈 `edits`의 `out_of_scope`에 `_text`의 기본 비어 있지 않은 문자열 검증을 적용한다. `publish`는 dry-run 분기 및 외부 작업 전에 이를 호출한다. 정상 발행 테스트 데이터에도 제외 범위를 명시해야 한다. 검증: `uv run pytest tests/acceptance/test_publish_out_of_scope.py`; 신규·수정 및 일반·dry-run 거절 회귀 테스트는 `tests/test_order.py -k empty_out_of_scope`.
- #111: 주문 인터뷰의 기준은 `docs/decisions.md` D58과 보강 2다. 스타일은 사람이 확인해 장부에 고정하고, 도구·검토·분야 스킬은 승인 목록에서 레인 에이전트가 이슈마다 고른다. `fa skills add SOURCE --approve`는 발견한 모든 스킬에 `--role SKILL=ROLE`이 필요하다. `fa order publish`는 스펙·인수 시험만 커밋하므로 결정 기록 변경까지 자동 발행한다고 안내하면 안 된다.
- #111 검증: `uv run pytest tests/acceptance/test_fa_order_interview.py tests/acceptance/test_fa_order_skill.py`. 선택적 Ouroboros 분기는 유지한다. 이 인수 시험은 명령 주변의 분기 문맥도 읽으므로, native 폴백 설명에서 외부 인터뷰 명령을 무조건 실행하는 것처럼 쓰지 않는다.
