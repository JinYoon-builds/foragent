---
name: acorn-interact-170
description: #170 clickable lane rows — fold rules, TIGHT plain-frame trimming, state keys, scrolling window, live OSC 8 and the locked-test conflicts it had to reconcile
metadata:
  type: project
---

#170 상호작용: 키·마우스·스크롤은 `watch_mouse.py`(handle_key·handle_mouse·scroll·scroll_limit·highlighted·osc8·terminal_setup/restore_sequences), 이슈 상자는 `watch_sections/acorns.py`(ORDER 6, issue_box·issue_url), 문구는 `messages/acorns.py`. state 키: issue·store(상자를 열 때만 저장)·scroll·unfolded·length·rows[(y,번호)]·height·issue_lanes·lanes. `_window`(watch_layout)가 render_frame(height=)과 실시간 화면을 같이 자른다.

**Why:** 고정 시험 충돌 두 개를 구현으로 풀었다. ① `test_view_done_state`는 끝난 주문의 쉬는 레인에 ▸가 없어야 함 → 끝난 주문 구역(done.py)이 이미 나열한 병합은 숨긴 줄로 세지 않는다. ② `test_view_sections`는 running_snapshot이 24줄에 FIRST·LAST 구역과 함께 들어가야 함 → `TIGHT = True` 구역(lanes)을 `tight=True`로 다시 그려 접힌 레인의 마지막 완료 줄·토글을 다른 구역보다 먼저 뺀다. `test_lane_view_d76`은 아무것도 안 하는 클릭이 state를 바꾸면 실패한다(그래서 store는 열 때만 저장).

**How to apply:** 실시간 화면은 `_frame(..., height=h, scrolls=True)`로 구역을 빼지 않고 STAYS만 옮긴다. 줄 검사 일반 시험은 `_frame(height=None)`으로 24줄 예산을 피한다. 실제 TTY 확인은 pty.fork로 자식에서 watch.run을 돌리고 키를 보내 1049h/l·OSC 8·위치 줄을 찾았다. 검증: `fa test tests/acceptance/test_acorn_interact.py tests/test_watch*.py tests/acceptance/test_squirrel_running.py tests/acceptance/test_view_sections.py tests/acceptance/test_view_done_state.py tests/acceptance/test_lane_view_d76.py` (335 passed와 같은 묶음). 화면 인수 63개 파일 비교에서 새 실패 0.
