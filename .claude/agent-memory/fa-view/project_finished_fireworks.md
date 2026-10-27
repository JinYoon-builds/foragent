---
name: project-finished-fireworks
description: #168 finished-order fireworks band — section rules (GAP_AFTER, YIELD_BEFORE), state/calm plumbing, and the fixed render_frame argument trap
metadata:
  type: project
---

#168 불꽃 구역은 `watch_sections/finished.py`(ORDER 22)이고 done은 23, summary는 24로 밀렸다. `Config.animation`(bool, 기본 True)을 이 이슈가 만들었다. false면 squirrels·stage의 큰 다람쥐 그림을 숨긴다(stage는 말풍선 한 줄만 남김).

- 섹션 문맥에 `height`·`state`가 추가됐다. `_frame(..., state=)`와 `render_frame(state=)`가 전달하고, 대화형 `_terminal`은 루프 밖 `view_state` dict를 넘긴다. 불꽃 시작 tick은 `state["fireworks"][order 번호]`에 기록한다. state가 없으면 tick 0부터 센다(고정 시험 조건).
- 새 섹션 속성: `GAP_AFTER = False`는 다음 섹션과 빈 줄을 없앤다. `YIELD_BEFORE = "done"`은 짧은 화면에서 그 형제 섹션 바로 앞에 사라진다. 24줄 기본 화면에서 불꽃이 보여야 하므로(test_squirrel_done) 이 둘이 필요했다. "불꽃이 맨 먼저 사라진다"를 글자 그대로 구현하면 고정 시험이 실패한다.
- **Why:** `test_watch_split`·`test_watch_open`·`test_watch_reload_runtime`의 가짜 `render_frame(*args)`는 키워드 인자를 받지 않고 `(snapshot, config, repo_root)`와 정확히 비교한다. 그래서 `--once`의 차분한 화면은 인자가 아니라 `finished.calm()` contextvar로 알린다. `test_watch_compat`의 `_frame` 호출 기대값에는 `state={}`를 추가했다.
- **How to apply:** `watch.run`의 once 경로와 `render_frame` 인자를 바꿀 때 이 고정 가짜 함수를 먼저 확인한다. 검증: `fa test tests/acceptance/test_squirrel_done.py tests/test_watch_finished.py`. 화면 관련 묶음에서 기준본(HEAD)과 같은 20개 실패(acorn_interact·dream_view·lane_talk_view·view_usage_line·view_ux_copy, 다른 이슈 범위)를 확인했다.
