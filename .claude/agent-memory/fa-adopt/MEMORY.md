# adopt 레인 현재 메모

- `fa adopt` 하위 명령의 인자와 오류 처리는 `src/foragent/cli.py`에 이미 있다. 구현은 `src/foragent/adopt.py`의 `run()`에서 분기한다.
- `load_config()`는 `.foragent.yml`이 없으면 오류를 낸다. 초기 도입 전 survey는 파일이 없을 때 `Config()` 기본값을 사용한다. 기본 인수 시험 경로는 `tests/acceptance/`다.
- survey의 빈 저장소 판정은 추적 파일 기준이고, 기존 에이전트 설정과 CI는 현재 작업 폴더 기준이다. 같은 경로의 여러 파일이 한 커밋에 바뀌어도 변경 횟수는 한 번이다.
- Git 파일명은 NUL 구분자로 읽는다. `git log --name-only -z`는 커밋 헤더 뒤 첫 파일명 앞에 줄바꿈을 추가한다. 파일명 자체의 공백이나 줄바꿈을 일괄 제거하면 안 된다.
- CI 파일 하나에 여러 테스트 실행이 있으면, 하나라도 인수 시험을 포함할 때 `runs_acceptance`는 true다. pytest의 정확한 `--ignore=<acceptance_dir 끝 슬래시 제거>` 인자가 제외 근거다.
- 사람이 확인하는 명령: `uv run fa adopt survey --json`, `uv run fa adopt survey`. 인수 시험 명령: `uv run pytest -q tests/acceptance/test_adopt_survey.py`. issue #3에서는 사용자 지시에 따라 테스트를 실행하지 않았다.
- 로컬 기본 `python3`에는 PyYAML이 없었다. 환경을 만들지 않고 CLI를 확인할 때 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/apple/.local/share/uv/tools/foragent/bin/python -B -m foragent.cli adopt survey`를 사용했다.
- issue #5의 CI 조회·수정은 `ci(root, exclude_acceptance=False)`가 맡는다. survey와 `_workflow_runs()`·`_test_invocations()`를 공유한다. CI 조회에는 Git 초기화나 `.foragent.yml`이 필요 없다.
- YAML을 다시 저장하거나 `read_text()`로 개행을 정규화하면 바이트 보존 조건을 깨뜨린다. `read_bytes().decode("utf-8")`와 YAML scalar 위치를 사용해 뒤쪽부터 옵션만 삽입한다. 따옴표·주석·CRLF·마지막 개행 유무를 보존한다. alias·merge는 같은 원문 위치에 한 번만 삽입한다.
- 셸 주석과 따옴표 안의 `#`를 구별해야 한다. 따옴표를 제거한 토큰에서 `#`를 주석으로 판단하면 이미 붙인 `--ignore`를 못 보고 반복 실행 때 중복 추가한다.
- CI의 npm/pnpm/go 실행 및 여러 줄 YAML 실행 블록은 `manual`에 표시한다. `needs_change`는 수정 전 상태이고 `changed`는 이번 호출이 실제 바꾼 파일이다.
- 검증: `uv run pytest -q tests/acceptance/test_adopt_ci.py tests/acceptance/test_adopt_survey.py tests/test_adopt.py tests/test_cli.py` (issue #5: 84개 통과). 기본 uv 캐시에 쓰기가 막히면 `/private/tmp`에 캐시 메타데이터와 해당 패키지 archive를 복사하고 `UV_CACHE_DIR`로 지정해 `uv run --offline`을 쓸 수 있다. 외부 archive를 가리키는 symlink만 복사하면 캐시가 인식되지 않았다.
