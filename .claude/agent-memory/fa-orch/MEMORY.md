# orch 레인 현재 메모

- gate·runner의 차단 처리는 `src/foragent/actions.py`의 `flag()`를 공유한다. 이 함수는 `blocked` 라벨과 `<!-- foragent:blocked reason=... -->` 댓글을 남기며, 기존 이유 코드·상세 출력·승인 무효화 기록을 유지한다.
- `format_comment()`의 기본은 기존 사람 확인 댓글과 호환되도록 `needs-human`이다. gate·runner에서는 `label="blocked"`를 명시한다. `parse_reason()`은 두 표식을 모두 읽는다.
- `scheduler.tick()`의 타임아웃은 로컬 flag 기록이 아니라 GitHub에서 가장 최근의 blocked 댓글 시각을 기준으로 한다. 알 수 없는 새 이유 코드도 유효한 blocked 표식이면 시계 기준이 된다. `orchestrator_timeout_minutes` 기본값은 30이며 양의 정수만 허용한다.
- 재시작·중단 복구와 실행 직전 검사에도 `blocked`를 포함해야 대기 이슈가 다시 실행되지 않는다. 기존 hash-mismatch의 유효한 재승인 경로는 두 대기 라벨을 해제한 뒤 스냅샷을 갱신한다.
- inspect는 이슈 목록 외에 열린 PR도 따로 수집한다. blocked 이슈의 PR이 이 우회 경로로 사람 검토 목록에 나오지 않도록 함께 확인한다.
- #28 인수 시험: `uv run pytest -q tests/acceptance/test_orch_blocked.py`. acceptance 파일은 수정하지 않는다. 임시 Git 이력과 가짜 실행기로 네트워크 없이 검증한다.
- #28 검증 결과: scheduler·runner·config·reasons·inspect 및 지정 인수 시험 781개, `tests/test_orch_blocked.py`의 경계 시험 10개 통과. 경계 시험은 Git 없이 가짜 tracker와 주입 시계로 실행한다.
- 이 환경에서 기본 uv 캐시에 쓰기 제한이 있으면, 패키지가 준비된 임시 캐시로 `UV_CACHE_DIR=/private/tmp/fa-issue-4-uv-cache uv run --offline pytest ...`를 실행할 수 있다.
- 판정 불가 리뷰는 리뷰어만 다시 호출하지 않고 구현 시도를 하나 소비한다. `build_prompt(..., reviewer_text=...)`가 `A reviewer said (verbatim):` 뒤에 원문 전체를 넣는다. 일반 검사 실패의 4,000자 꼬리 제한을 이 경로에 적용하면 안 된다.
- 반박은 `.foragent/REBUTTAL.md`에서 읽어 PR의 `## Rebuttals`와 다음 리뷰에 전달한다. Claude는 임시 `CLAUDE.local.md`, Codex는 리뷰 프롬프트를 쓴다. `GitRepo.stage_changes()`는 실행기가 직접 스테이징한 `.foragent` 파일도 인덱스에서 제거하므로 기존 경로를 공유한다.
- batch/fast 대상 전환은 worktree를 다시 만들기 때문에 `TRY.md`와 `REBUTTAL.md`를 함께 보존해야 한다. PR에는 각 세션의 서로 다른 반박 내용을 보존한다.
- `review-dispute`는 반박이 있는 상태로 진행한 리뷰 두 회에서 연속으로 같은 지적이 나왔을 때의 기술 차단이다. 공백·대소문자·목록 기호를 정규화한다. 비정형 원문은 하나의 지적으로 비교하여 공통 서문만으로 분쟁을 만들지 않는다.
- #29 인수 시험: `uv run pytest -q tests/acceptance/test_orch_review_handoff.py`. `tests/test_orch_blocked.py`와 `tests/acceptance/test_orch_blocked.py`는 모듈명이 같아 기본 pytest 모드로 함께 수집하면 충돌하므로 별도로 실행한다.
