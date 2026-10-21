# adopt 레인 현재 메모

- `fa adopt` 하위 명령의 인자와 오류 처리는 `src/foragent/cli.py`에 이미 있다. 구현은 `src/foragent/adopt.py`의 `run()`에서 분기한다.
- `load_config()`는 `.foragent.yml`이 없으면 오류를 낸다. 초기 도입 전 survey는 파일이 없을 때 `Config()` 기본값을 사용한다. 기본 인수 시험 경로는 `tests/acceptance/`다.
- survey의 빈 저장소 판정은 추적 파일 기준이고, 기존 에이전트 설정과 CI는 현재 작업 폴더 기준이다. 같은 경로의 여러 파일이 한 커밋에 바뀌어도 변경 횟수는 한 번이다.
- Git 파일명은 NUL 구분자로 읽는다. `git log --name-only -z`는 커밋 헤더 뒤 첫 파일명 앞에 줄바꿈을 추가한다. 파일명 자체의 공백이나 줄바꿈을 일괄 제거하면 안 된다.
- CI 파일 하나에 여러 테스트 실행이 있으면, 하나라도 인수 시험을 포함할 때 `runs_acceptance`는 true다. pytest의 정확한 `--ignore=<acceptance_dir 끝 슬래시 제거>` 인자가 제외 근거다.
- 사람이 확인하는 명령: `uv run fa adopt survey --json`, `uv run fa adopt survey`. 인수 시험 명령: `uv run pytest -q tests/acceptance/test_adopt_survey.py`. issue #3에서는 사용자 지시에 따라 테스트를 실행하지 않았다.
- 로컬 기본 `python3`에는 PyYAML이 없었다. 환경을 만들지 않고 CLI를 확인할 때 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/apple/.local/share/uv/tools/foragent/bin/python -B -m foragent.cli adopt survey`를 사용했다.
- baseline의 자동 감지 순서는 Python(`pyproject.toml` 또는 `requirements*.txt`) → package.json의 test 스크립트 → Go다. 명령은 각각 `uv run pytest`, `npm test`/`pnpm test`, `go test ./...`이며, pnpm은 잠금 파일 또는 `packageManager: pnpm@...`로 고른다. order 레인 파일을 수정하지 않고 adopt 안에서 감지한다.
- baseline은 `.foragent.yml`이 없어도 실행한다. pytest에는 끝 슬래시를 뺀 `--ignore=<acceptance_dir>`와 `-rf`를 추가하고, 공백이 있는 경로는 셸 인용한다. JSON stdout에 테스트 실행 로그를 섞지 않는다. 비-pytest의 실패 목록은 항상 빈 배열이다.
- `foragent.processes.run_logged()`가 실행 제한 시 자식 프로세스 그룹까지 종료한다. 임시 로그 파일을 사용하면 기존 실행기를 재사용하면서 pytest의 전체 `FAILED` 요약을 읽을 수 있다. `capture_stdout=True`의 반환 문자열은 꼬리만 담으므로 실패 목록 파싱에 쓰지 않는다.
- 검증: `uv run pytest -q tests/test_adopt.py tests/acceptance/test_adopt_baseline.py tests/acceptance/test_adopt_survey.py` → 20 passed. 이 작업 환경에서는 기본 uv 캐시 쓰기가 막혀 `/private/tmp/fa-issue-4-uv-cache`로 필요한 기존 wheel 캐시와 연결된 archive를 복사한 뒤 `UV_CACHE_DIR`로 지정했다. 네트워크 없이 로컬 의존성으로 실행 가능했다.
