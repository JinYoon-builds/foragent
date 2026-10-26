---
name: single-lane-view-lock
description: #543 lane view flock (watch.lock), shared watch-open.json spacing for all openers, execv lock handoff via env fd
metadata:
  type: project
---

#543 대화형 `watch.run`은 `watch_open.exclusive(store)`로 `<state>/watch.lock`에 flock을 잡은 뒤에만 `registered`(watch.pid)를 쓴다. 못 잡으면 `watch_already_open` 한 줄 출력 후 0으로 끝난다. `--once`는 잠그지 않는다.

- 열기 판정 `_live` = `_locked`(자기 descriptor로 LOCK_NB 시험 후 즉시 닫음) 또는 기존 `_running`(watch.pid).
- `open_view`는 live → 건너뜀, `watch-open.json`이 REOPEN_SPACING 안이면 건너뜀, 아니면 `_launch(..., now)`. 직접 열기는 마지막 split/window 명령(`final`)이 실패·시간 초과일 때만 기록한다. orca `terminal show` 실패는 기록하지 않는다.
- `reopen`은 예전처럼 시도 전에 기록하고 모듈의 `open_view`를 부른다(`tests/test_scheduler.py`가 `watch_open.open_view`를 3인자 가짜로 바꿈). 방금 쓴 기록에 막히지 않게 `_reopening` ContextVar로 표시한다. 우회하면 시험에서 진짜 osascript가 돌아 SIGKILL 금지에 걸린다. 열 도구가 없어 수동 명령만 출력하면 ContextVar의 dict에 표시하고 reopen이 이전 기록으로 되돌린다(수동 출력은 시도가 아님, 리뷰 지적).
- 열기 직렬화: `_opening(store)`가 `watch-open.lock`에 blocking flock을 잡고 live·spacing 확인부터 마지막 명령·기록까지 한 번에 한다(리뷰: show 대기 중 다른 열기의 시간 초과 기록 우회). reopen이 잠금을 잡고 ContextVar로 open_view에 알리므로 open_view는 다시 잡지 않는다(같은 프로세스에서 두 번 열면 교착). 회귀: `tests/test_watch_open.py -k concurrent`.
- 빌드 재실행: 잠금 fd는 inheritable이고 execv 직전 `FORAGENT_WATCH_LOCK_FD`에 번호를 넣는다. 새 프로세스의 `_inherited_lock`이 inode를 비교해 같은 open file description을 재사용한다(새로 open하면 자기 자신과 충돌).
- 시험은 `watch_open.datetime`을 가짜 시계로 바꾸므로 `datetime.now(UTC)`를 모듈 이름으로 부른다.
- 검증: Done-when 27 passed, 열기 관련 57 passed, `tests/test_scheduler.py -k view` 12 passed.
