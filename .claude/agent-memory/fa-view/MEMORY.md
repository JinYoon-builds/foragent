# View lane 기억

- 레인 이름의 원본은 `.foragent.yml`의 `lanes.<slug>.title`·`emoji`. 사람용 문자열은 `foragent.labels.lane_display(config, slug, language)`로 만들고, JSON의 slug와 라벨·에이전트 파일 이름은 유지한다.
- `fa gate status`는 `cli._status` → `_print_status`, `fa inspect`는 `inspect_data.collect` → `cli._inspect` 흐름이다. inspect의 완료 항목은 열린 이슈 목록에서 사라지므로 저장된 실행 기록의 lane을 우선 사용한다.
- 이름 관련 검증: `uv run pytest -q tests/acceptance/test_view_names.py tests/test_lane_display.py tests/test_cli.py tests/test_config.py tests/test_skills_lanes.py tests/test_inspect.py`. 인수 테스트는 수정하지 않는다.
- 제한된 작업 환경에서는 `UV_CACHE_DIR=/tmp/foragent-issue-33-uv`로 uv 캐시를 쓰기 가능한 곳에 둔다. 기본 캐시에 쓰지 못하거나 네트워크가 없을 수 있다. 로컬 패키지를 재사용할 때 PyYAML 휠의 Python 버전(cp312 등)을 실행기와 맞춘다.
- 상태 집계는 `status.collect_lanes`에서 한다. JSON의 `lanes`는 slug 키 사전이며 기존 `running`·`ready` 목록을 유지한다. 레인 대표 작업은 실행 중인 작업을 우선하며, `summary`는 같은 레인의 대기 작업도 모두 센다. `needs-human`·`blocked`는 남아 있는 `running` 라벨보다 우선한다.
- runner의 `last_progress_at`은 생존 확인에도 쓰인다. 화면의 `last_activity_at`은 실행기 로그가 커졌을 때만 갱신하며, 짧은 실행을 놓치지 않도록 종료 때도 로그 크기를 확인한다. 새 Run 필드는 기본값을 둬 이전 기록과 호환한다.
- #34 검증: `UV_CACHE_DIR=/tmp/foragent-issue-38-uv-cache uv run --offline pytest -q tests/acceptance/test_view_status.py`. 이 캐시는 pytest 9.1.1·Pygments까지 있어 네트워크 없이 실행할 수 있었다.
- `finish_run`은 종료된 기록의 단계·시각을 이력으로 보존한다. `status.collect_lanes`의 현재 단계·경과·마지막 움직임은 `run.status == "running"`인 실행에서만 가져온다. PR 검토 대기·blocked·needs-human과 남은 running 라벨의 조합은 `tests/test_status.py -k finished_run_does_not_show_stale_phase_or_ages`로 검증한다.

- #36 알림의 공통 진입점은 `notify.send(config, title, line, link)`다. 설정 누락·`None`은 `claude-push`로 해석하며 `<state>/push-outbox.jsonl`에 `title`·`line`·`link`를 기록한다. CLI와 inspect는 GitHub 원격에서 구한 repo를 config에 채워 전달한다.
- `notify.send`는 실패를 로그로 남기고 `False`를 반환한다. inspect는 성공했을 때만 중복 방지 서명을 저장하므로 실패 후 재시도가 가능하다. inspect가 이미 `notify_lock`을 잡고 있어 outbox는 파일 자체를 잠가 추가한다(같은 알림 잠금을 다시 잡으면 교착).
- #36 검증: `UV_CACHE_DIR=/tmp/foragent-issue-38-uv-cache uv run --offline pytest -q tests/acceptance/test_view_notify.py tests/test_notify.py tests/test_inspect.py tests/test_cli.py tests/test_config.py tests/test_launcher.py`. 실제 외부 전송 없이 채널별 형식·실패 격리·재시도를 검증한다.

- #35 `fa gate watch`는 `cli._status` 데이터를 `watch.collect`로 보완한다. `status.collect_lanes`의 `queued` 목록은 `ready`뿐 아니라 승인 후 의존성을 기다리는 작업도 포함한다. 기존 JSON의 `running`·`ready`는 유지한다.
- watch 주문 진행률은 실행 중인 작업의 열린 부모 이슈와 native `sub_issues`를 우선한다. 주문별 완료 실적은 `StateStore.read_history()` 전체를 쓰고 저장된 실행 lane을 따른다. status JSON의 `history`는 최근 10건뿐이라 완료 집계 원본으로 쓰면 안 된다.
- watch는 표준 `curses`로 0.3초마다 그리며 상태 조회는 별도 스레드에서 5초마다 한다. `--once`와 실시간 화면은 같은 배치를 공유하고, 한글·이모지는 터미널 셀 너비로 계산한다. 레인이 10개 이상이면 번호+Enter로 상세를 토글한다. 사건 파일의 작성 중인 마지막 JSON 줄은 건너뛴다.
- #35 검증: `UV_CACHE_DIR=/tmp/foragent-issue-38-uv-cache uv run --offline pytest -q tests/acceptance/test_view_watch.py tests/test_watch.py tests/test_status.py tests/test_cli.py`. 실제 TTY는 임시 FakeTracker 시나리오로 배지·타격 프레임·상세 토글·종료까지 확인한다.

- #37 `fa --version`은 `foragent._build.GIT_SHA`를 읽으며 모듈이 없으면 `unknown`이다. 실행 시 Git을 호출하지 않는다. `setuptools.build_meta`가 `setup.py`를 실행해 일반·editable 설치와 sdist 빌드 때 `_build.py`를 생성한다. 생성 파일은 gitignore 대상이다.
- sdist에는 `_build.py`가 포함된다. `.git`이 없는 배포본을 다시 빌드할 때는 저장된 SHA를 보존하고 상위 폴더의 다른 Git 저장소를 조회하지 않는다. uv 캐시 키에는 `setup.py`와 Git 커밋을 포함한다.
- #37 검증: `UV_CACHE_DIR=/tmp/foragent-issue-38-uv-cache uv run --offline pytest -q tests/acceptance/test_view_version.py tests/test_build_metadata.py tests/test_cli.py` (69개). `uv build --offline`로 sdist→wheel을 만든 뒤 `/tmp`의 별도 환경에 설치하고 `PATH=/nonexistent <환경>/bin/fa --version`으로 Git 없이 저장된 SHA가 나오는 것도 확인했다. 오프라인 캐시의 setuptools를 재사용할 때는 simple 인덱스·wheel 메타데이터뿐 아니라 archive도 새 캐시에 복사하고 symlink를 새 archive로 연결해야 한다.
