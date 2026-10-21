# View lane 기억

- 레인 이름의 원본은 `.foragent.yml`의 `lanes.<slug>.title`·`emoji`. 사람용 문자열은 `foragent.labels.lane_display(config, slug, language)`로 만들고, JSON의 slug와 라벨·에이전트 파일 이름은 유지한다.
- `fa gate status`는 `cli._status` → `_print_status`, `fa inspect`는 `inspect_data.collect` → `cli._inspect` 흐름이다. inspect의 완료 항목은 열린 이슈 목록에서 사라지므로 저장된 실행 기록의 lane을 우선 사용한다.
- 이름 관련 검증: `uv run pytest -q tests/acceptance/test_view_names.py tests/test_lane_display.py tests/test_cli.py tests/test_config.py tests/test_skills_lanes.py tests/test_inspect.py`. 인수 테스트는 수정하지 않는다.
- 제한된 작업 환경에서는 `UV_CACHE_DIR=/tmp/foragent-issue-33-uv`로 uv 캐시를 쓰기 가능한 곳에 둔다. 기본 캐시에 쓰지 못하거나 네트워크가 없을 수 있다. 로컬 패키지를 재사용할 때 PyYAML 휠의 Python 버전(cp312 등)을 실행기와 맞춘다.
