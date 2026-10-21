# orch 레인 현재 메모

- gate·runner의 차단 처리는 `src/foragent/actions.py`의 `flag()`를 공유한다. 이 함수는 `blocked` 라벨과 `<!-- foragent:blocked reason=... -->` 댓글을 남기며, 기존 이유 코드·상세 출력·승인 무효화 기록을 유지한다.
- `format_comment()`의 기본은 기존 사람 확인 댓글과 호환되도록 `needs-human`이다. gate·runner에서는 `label="blocked"`를 명시한다. `parse_reason()`은 두 표식을 모두 읽는다.
- `scheduler.tick()`의 타임아웃은 로컬 flag 기록이 아니라 GitHub에서 가장 최근의 blocked 댓글 시각을 기준으로 한다. 알 수 없는 새 이유 코드도 유효한 blocked 표식이면 시계 기준이 된다. `orchestrator_timeout_minutes` 기본값은 30이며 양의 정수만 허용한다.
- 재시작·중단 복구와 실행 직전 검사에도 `blocked`를 포함해야 대기 이슈가 다시 실행되지 않는다. 기존 hash-mismatch의 유효한 재승인 경로는 두 대기 라벨을 해제한 뒤 스냅샷을 갱신한다.
- inspect는 이슈 목록 외에 열린 PR도 따로 수집한다. blocked 이슈의 PR이 이 우회 경로로 사람 검토 목록에 나오지 않도록 함께 확인한다.
- #28 인수 시험: `uv run pytest -q tests/acceptance/test_orch_blocked.py`. acceptance 파일은 수정하지 않는다. 임시 Git 이력과 가짜 실행기로 네트워크 없이 검증한다.
- #28 검증 결과: scheduler·runner·config·reasons·inspect 및 지정 인수 시험 781개, `tests/test_orch_blocked.py`의 경계 시험 10개 통과. 경계 시험은 Git 없이 가짜 tracker와 주입 시계로 실행한다.
- 이 환경에서 기본 uv 캐시에 쓰기 제한이 있으면, 패키지가 준비된 임시 캐시로 `UV_CACHE_DIR=/private/tmp/fa-issue-4-uv-cache uv run --offline pytest ...`를 실행할 수 있다.
