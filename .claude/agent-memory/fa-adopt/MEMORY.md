# adopt 레인 현재 메모

## 구조
- `fa adopt` 하위 명령의 인자와 오류 처리는 `src/foragent/cli.py`에 있다. 구현은 `src/foragent/adopt.py`의 `run()`에서 분기한다. `order.py`는 다른 레인 소유다.
- `load_config()`는 `.foragent.yml`이 없으면 오류를 낸다. 도입 전 명령(survey 등)은 파일이 없을 때 `Config()` 기본값을 쓴다. 기본 인수 시험 경로는 `tests/acceptance/`다.
- `skills/fa-init/SKILL.md`의 기존 저장소 절차(issue #6): `fa adopt survey --json` → `kind`가 `existing`이면 "What I read" 목록 → 레인 → baseline → CI 질문(AskUserQuestion) → `/fa-order`. `empty`면 새 프로젝트 단계로 바로 건너뛴다. CI 수정·커밋은 사용자의 명시적 동의 뒤에만 한다. 절차를 고칠 때 이 순서와 새 프로젝트 경로를 함께 지킨다.

## survey
- 빈 저장소 판정은 추적 파일 기준이고, 기존 에이전트 설정과 CI는 현재 작업 폴더 기준이다. 같은 경로의 여러 파일이 한 커밋에 바뀌어도 변경 횟수는 한 번이다.
- Git 파일명은 NUL 구분자로 읽는다. `git log --name-only -z`는 커밋 헤더 뒤 첫 파일명 앞에 줄바꿈을 추가한다. 파일명 자체의 공백·줄바꿈을 일괄 제거하면 안 된다.

## ci
- `ci(root, exclude_acceptance=False)`가 조회·수정을 맡고 survey와 `_workflow_runs()`·`_test_invocations()`를 공유한다. Git 초기화나 `.foragent.yml`이 필요 없다.
- `_test_invocations()`는 정규화된 시험 명령만 반환한다(`uv run pytest` → pytest). `_test_commands()`는 `(원래 실행 접두사 토큰, 정규화한 시험 명령 토큰)`을 반환하며, 접두사는 uv/poetry 옵션·`python -m`·실행 경로를 보존하고 `shlex.join()`으로 인용한다.
- CI 파일 하나에 여러 테스트 실행이 있으면, 하나라도 인수 시험을 포함할 때 `runs_acceptance`는 true다. pytest의 정확한 `--ignore=<acceptance_dir 끝 슬래시 제거>` 인자가 제외 근거다.
- YAML을 다시 저장하거나 `read_text()`로 개행을 정규화하면 바이트 보존 조건이 깨진다. `read_bytes().decode("utf-8")`와 YAML scalar 위치로 뒤쪽부터 옵션만 삽입한다. 따옴표·주석·CRLF·마지막 개행 유무를 보존한다. alias·merge는 같은 원문 위치에 한 번만 삽입한다.
- 셸 주석과 따옴표 안의 `#`를 구별한다. 따옴표를 제거한 토큰에서 `#`를 주석으로 보면 이미 붙인 `--ignore`를 못 보고 반복 실행 때 중복 추가한다.
- npm/pnpm/go 실행과 여러 줄 YAML 실행 블록은 `manual`에 표시한다. `needs_change`는 수정 전 상태이고 `changed`는 이번 호출이 실제 바꾼 파일이다.
- `collect_only`(issue #66, D52)는 워크플로별 첫 pytest 접두사에 ` --collect-only -q`를 붙인 제안 목록이다. 같은 워크플로에 이미 `--collect-only` 실행이 있으면 제안하지 않는다. 조회는 파일을 바꾸지 않는다. `--exclude-acceptance`는 수집 전용 명령에 제외 옵션을 붙이지 않는다. fa-init은 수집 제안을 보여 주기만 하고 자동 삽입하지 않는다.

## baseline
- `baseline(root, command=None, timeout=900)`. `_detect_test_command()` 순서는 Python(`pyproject.toml` 또는 `requirements.txt`) → npm/pnpm(`package.json` test 스크립트) → Go다. Python은 order와 같은 `uv run pytest`를 쓴다.
- pytest 여부는 원래 argv의 첫 토큰이 아니라 정규화된 명령으로 판단한다. issue #4는 리뷰에서 `invocation[0] == "pytest"` 판정이 `uv run pytest`에 대해 늘 거짓이 될 수 있다는 지적으로 한 번 보류됐다(그러면 `--ignore`·`-rf`가 빠지고 `failing`이 늘 빈다). 래퍼(uv/poetry/python -m)를 거친 명령으로 시험한다.
- pytest 실행 부분에 설정 경로의 `--ignore`와 `-rf`를 붙인다. 자식 출력을 수집해 JSON에 섞이지 않게 하고, ANSI 색상을 제거한 `FAILED ` 요약 줄에서 node id를 추출한다. 매개변수에 공백이 있을 수 있으므로 첫 공백으로 자르지 않는다.
- 시간 초과 때 셸만 종료하면 자식 테스트가 출력 파이프를 계속 잡는다. 별도 프로세스 그룹을 만들어 전체를 종료하고 `exit_code: 124`, `passed: false`로 보고한다. 실패·시간 초과·명령 없음 모두 fa 종료 코드는 0이다.

## 검증·환경
- 사람 확인 명령: `uv run fa adopt survey --json`, `fa adopt ci --json`, `fa adopt baseline --json`. 이슈별 확인 안내는 `.foragent/TRY.md`에 쓴다.
- 레인 회귀 묶음: `uv run --offline pytest -q tests/acceptance/test_ci_collect.py tests/acceptance/test_adopt_ci.py tests/acceptance/test_adopt_survey.py tests/acceptance/test_adopt_baseline.py tests/test_adopt.py tests/test_cli.py` (issue #66 기준 116개 통과). 기존 CI 범위는 `uv run --offline pytest -q --ignore=tests/acceptance`.
- 샌드박스에서 기본 uv 캐시 쓰기가 막히면 `/private/tmp`에 캐시 메타데이터와 해당 패키지 archive를 실제로 복사하고 `UV_CACHE_DIR`로 지정해 `uv run --offline`을 쓴다(예: `/private/tmp/fa-issue-4-uv-cache`). 외부 archive를 가리키는 symlink만 복사하면 캐시가 인식되지 않는다.
- 로컬 기본 `python3`에는 PyYAML이 없다. 환경 없이 CLI를 볼 때: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/apple/.local/share/uv/tools/foragent/bin/python -B -m foragent.cli adopt survey`.
