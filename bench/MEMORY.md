# Bench lane 기억

- CLI 연결은 `src/foragent/cli.py`에 있다. 구현은 `src/foragent/bench.py`에서 분기하며 인수 테스트는 수정하지 않는다.
- `report`는 시나리오 정의를 읽거나 검사를 실행하지 않고 해당 디렉터리의 `results.jsonl`만 읽는다. 파일 누락은 빈 이력이다.
- 결과는 항상 A, B, C 순서다. 기록 수는 해당 arm의 전체 행 수이고, 여섯 측정값의 중앙값은 항목마다 null을 제외하여 따로 계산한다. 0은 유효한 값이다.
- JSON의 최상위 키는 `arms`이며 누락 측정값은 null이다. Markdown 표는 머리글·구분선·세 arm 행으로 구성하고 누락 측정값은 `—`로 표시한다.
- 검증: `uv run pytest -q tests/acceptance/test_bench_report.py tests/acceptance/test_bench_record.py tests/test_bench.py`.
- 기본 uv 캐시 쓰기가 제한되면 `UV_CACHE_DIR=/private/tmp/foragent-issue-10-uv-cache`처럼 쓰기 가능한 경로를 사용한다. 다운로드 DNS 오류는 테스트 실패와 구분한다. 기존 archive 캐시를 재사용할 때 PyYAML wheel의 Python 버전 태그가 실행 환경과 맞아야 한다.
