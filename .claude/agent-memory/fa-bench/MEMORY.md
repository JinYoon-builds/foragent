# Bench lane 기억

- 레인 기억은 이 파일(`.claude/agent-memory/fa-bench/MEMORY.md`) 하나다. #10이 `bench/MEMORY.md`에 따로 썼는데, 새 교훈은 여기에 적는다.
- CLI 인자와 분기는 `src/foragent/cli.py`에 이미 있다. bench 레인은 `src/foragent/bench.py`의 `run(args)` 아래 `_record`·`_report`를 고친다. 인수 테스트는 수정하지 않는다.
- 시나리오는 디렉터리의 `scenario.yml`이다. `name`, `brief`는 문자열, `checks`는 셸 명령 문자열 목록이다. YAML의 `false` 같은 명령은 따옴표로 감싸야 불리언이 되지 않는다.
- `record`: 검사마다 `sh -c`, `cwd=--repo`, `FA_BENCH_SCENARIO=<절대 시나리오 경로>`로 실행한다. 검사 실패는 측정값이므로 다음 검사와 기록을 계속한다. 결과는 시나리오 디렉터리의 `results.jsonl`에 한 줄씩 덧붙인다.
- `record`의 실행 이력은 `StateStore.read_history()`로 읽는다(`runs.jsonl`, 파일 누락은 빈 이력, 같은 `(number, started_at)` 중복은 마지막 값). 시간대가 있는 시각으로 `[started, finished]` 양끝을 포함해 비교한다.
- `report`: 시나리오 정의를 읽지 않고 검사도 돌리지 않는다. `results.jsonl`만 읽고, 파일 누락은 빈 이력이다. arm은 항상 A, B, C 순서다. 기록 수는 arm의 전체 행 수, 측정값 중앙값은 항목마다 null을 빼고 따로 계산한다(0은 유효값).
- `report` 출력: JSON 최상위 키는 `arms`, 누락 측정값은 null. Markdown 표는 머리글·구분선·arm 세 행이고 누락은 `—`.
- 검증: `uv run pytest -q tests/acceptance/test_bench_record.py tests/acceptance/test_bench_report.py tests/test_bench.py`.
- 실행 샌드박스는 네트워크가 막혀 `uv run`이 의존성 다운로드(DNS 오류)에서 실패한다. 이것은 테스트 실패가 아니다. 기본 uv 캐시도 쓰기 제한이므로 `UV_CACHE_DIR=/private/tmp/foragent-issue-<N>-uv-cache`를 쓴다. `--offline`도 캐시에 없는 wheel 때문에 실패했다. 통한 방법: `~/.cache/uv/archive-v0/*/`의 pytest·pluggy·pyyaml·iniconfig·packaging·pygments를 `.venv/lib/python3.12/site-packages`로 복사하고 `PYTHONPATH=src .venv/bin/python -m pytest -q ...`로 돌린다. PyYAML wheel의 Python 버전 태그는 venv(3.12)와 맞아야 한다.
