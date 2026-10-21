# View lane 기억

- 레인 이름의 원본은 `.foragent.yml`의 `lanes.<slug>.title`·`emoji`. 사람용 문자열은 `foragent.labels.lane_display(config, slug, language)`로 만들고, JSON의 slug와 라벨·에이전트 파일 이름은 유지한다.
- `fa gate status`는 `cli._status` → `_print_status`, `fa inspect`는 `inspect_data.collect` → `cli._inspect` 흐름이다. inspect의 완료 항목은 열린 이슈 목록에서 사라지므로 저장된 실행 기록의 lane을 우선 사용한다.
- 이름 관련 검증: `uv run pytest -q tests/acceptance/test_view_names.py tests/test_lane_display.py tests/test_cli.py tests/test_config.py tests/test_skills_lanes.py tests/test_inspect.py`. 인수 테스트는 수정하지 않는다.
- 제한된 작업 환경에서는 `UV_CACHE_DIR=/tmp/foragent-issue-33-uv`로 uv 캐시를 쓰기 가능한 곳에 둔다. 기본 캐시에 쓰지 못하거나 네트워크가 없을 수 있다. 로컬 패키지를 재사용할 때 PyYAML 휠의 Python 버전(cp312 등)을 실행기와 맞춘다.
- 상태 집계는 `status.collect_lanes`에서 한다. JSON의 `lanes`는 slug 키 사전이며 기존 `running`·`ready` 목록을 유지한다. 레인 대표 작업은 실행 중인 작업을 우선하며, `summary`는 같은 레인의 대기 작업도 모두 센다. `needs-human`·`blocked`는 남아 있는 `running` 라벨보다 우선한다.
- runner의 `last_progress_at`은 생존 확인에도 쓰인다. 화면의 `last_activity_at`은 실행기 로그가 커졌을 때만 갱신하며, 짧은 실행을 놓치지 않도록 종료 때도 로그 크기를 확인한다. 새 Run 필드는 기본값을 둬 이전 기록과 호환한다.
- #34 검증: `UV_CACHE_DIR=/tmp/foragent-issue-38-uv-cache uv run --offline pytest -q tests/acceptance/test_view_status.py`. 이 캐시는 pytest 9.1.1·Pygments까지 있어 네트워크 없이 실행할 수 있었다.
- `finish_run`은 종료된 기록의 단계·시각을 이력으로 보존한다. `status.collect_lanes`의 현재 단계·경과·마지막 움직임은 `run.status == "running"`인 실행에서만 가져온다. PR 검토 대기·blocked·needs-human과 남은 running 라벨의 조합은 `tests/test_status.py -k finished_run_does_not_show_stale_phase_or_ages`로 검증한다.
