---
name: done-state-section
description: #114 finished-order section (watch_sections/done.py), hidden empty panels, zero-hiding summary, idle-lane words and wrapping
metadata:
  type: project
---

#114 주문 완료(`snapshot.completed == snapshot.total > 0`)는 `watch_sections/done.py`(ORDER 12)가 그린다. 병합 시각은 `watch_data.collect`가 `local_issues()`의 `merged_at`으로 `Snapshot.merged_at`에 넣는다. progress의 `progress_done` 문구는 지웠다. activity는 일하는 레인이 없으면 표를, 사건이 없으면 기록 칸을 숨기고 남은 칸이 전체 너비를 쓴다. lanes의 ▶는 `links=bool(busy)`, 쉬는 레인 줄은 레인 사이에서 줄바꿈한다. summary의 0 숨김은 `messages/status.py`의 `summary` 틀을 ` · `로 나눠 쓴다(gate status는 그대로).

**Why:** 고정 test_view_done_state가 빈 표 제목·▶·`✓N`·중복 `확인할 일 0`이 없기를 요구한다.

**How to apply:** 옛 기대(`all done`, `none yet`, `✓1`, `모든 레인이 쉬는 중`)를 가진 일반 시험은 test_watch.py·test_watch_progress.py에 있다. 기준 대비 남는 실패 7개(test_view_usage_line 5, test_squirrel_done 2)는 다른 이슈 범위다. 검증: `fa test tests/acceptance/test_view_done_state.py tests/test_watch_done_state.py tests/test_watch.py tests/test_watch_progress.py`.

리뷰 보완: "줄바꿈" 요구가 있는 줄은 `_fit`으로 끝을 자르면 안 된다. 쉬는 레인은 `lanes._flow`(항목 사이에서 줄바꿈, 너무 긴 항목·제목은 `watch._wrap`으로 낱말 사이에서 줄바꿈), 완료 목록은 `done._wrapped`를 쓴다. 리뷰는 첫 항목·이어지는 항목·레인 이름이 줄보다 긴 경우를 각각 본다.

`watch._line`은 `_clean`으로 공백을 모아 앞 들여쓰기를 지운다. 들여쓴 줄은 부분을 먼저 `_clean`한 뒤 `[(text, style)]`로 직접 만든다. 시간 초과·GitHub 오류로 작업 트리가 다시 만들어지면 작업은 브랜치 커밋에 남고 `.foragent/` 파일(TRY·REBUTTAL·try 스크립트)만 다시 쓰면 된다.
