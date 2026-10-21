# adopt 레인 현재 메모

- `fa adopt` 하위 명령의 인자와 오류 처리는 `src/foragent/cli.py`에 이미 있다. 구현은 `src/foragent/adopt.py`의 `run()`에서 분기한다.
- `load_config()`는 `.foragent.yml`이 없으면 오류를 낸다. 초기 도입 전 survey는 파일이 없을 때 `Config()` 기본값을 사용한다. 기본 인수 시험 경로는 `tests/acceptance/`다.
- survey의 빈 저장소 판정은 추적 파일 기준이고, 기존 에이전트 설정과 CI는 현재 작업 폴더 기준이다. 같은 경로의 여러 파일이 한 커밋에 바뀌어도 변경 횟수는 한 번이다.
- Git 파일명은 NUL 구분자로 읽는다. `git log --name-only -z`는 커밋 헤더 뒤 첫 파일명 앞에 줄바꿈을 추가한다. 파일명 자체의 공백이나 줄바꿈을 일괄 제거하면 안 된다.
- CI 파일 하나에 여러 테스트 실행이 있으면, 하나라도 인수 시험을 포함할 때 `runs_acceptance`는 true다. pytest의 정확한 `--ignore=<acceptance_dir 끝 슬래시 제거>` 인자가 제외 근거다.
- 사람이 확인하는 명령: `uv run fa adopt survey --json`, `uv run fa adopt survey`. 인수 시험 명령: `uv run pytest -q tests/acceptance/test_adopt_survey.py`. issue #3에서는 사용자 지시에 따라 테스트를 실행하지 않았다.
- 로컬 기본 `python3`에는 PyYAML이 없었다. 환경을 만들지 않고 CLI를 확인할 때 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/apple/.local/share/uv/tools/foragent/bin/python -B -m foragent.cli adopt survey`를 사용했다.
