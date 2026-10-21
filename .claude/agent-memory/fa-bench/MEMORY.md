# Bench lane 기억

- CLI 인자와 분기는 `src/foragent/cli.py`에 이미 있다. bench lane에서는 `src/foragent/bench.py`의 `run(args)`를 구현하며, #9는 `record`만 담당한다.
- 시나리오는 디렉터리의 `scenario.yml`이며 `name`, `brief`는 문자열, `checks`는 셸 명령 문자열 목록이다. YAML의 `false` 같은 명령은 따옴표로 감싸야 불리언으로 해석되지 않는다.
- 검사마다 `sh -c`를 사용하고 `cwd=--repo`, `FA_BENCH_SCENARIO=<절대 시나리오 경로>`를 전달한다. 검사 실패는 측정값이며 후속 검사와 결과 기록을 계속한다.
- 실행 이력은 `StateStore.read_history()`로 읽는다. `runs.jsonl`만 사용하고 누락된 파일은 빈 이력으로 처리하며, 같은 `(number, started_at)`의 중복 완료 항목은 기존 저장소 규칙대로 마지막 값 하나만 반영한다. 시간대가 있는 시각으로 `[started, finished]` 양끝을 포함해 비교한다.
- 검증: `uv run pytest -q tests/acceptance/test_bench_record.py tests/test_bench.py`. 인수 테스트는 수정하지 않는다. 캐시 쓰기가 제한되면 `UV_CACHE_DIR=/private/tmp/foragent-issue-9-uv-cache`처럼 쓰기 가능한 위치를 사용한다.
