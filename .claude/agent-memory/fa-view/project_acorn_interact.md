---
name: acorn-interact-170
description: #170 clickable lane rows — fold rules, TIGHT plain-frame trimming, state keys, scrolling window, live OSC 8 and the locked-test conflicts it had to reconcile
metadata:
  type: project
---

#170 상호작용: 키·마우스·스크롤은 `watch_mouse.py`(handle_key·handle_mouse·scroll·scroll_limit·highlighted·osc8·terminal_setup/restore_sequences), 이슈 상자는 `watch_sections/acorns.py`(ORDER 6, issue_box·issue_url), 문구는 `messages/acorns.py`. state 키: issue·store(상자를 열 때만 저장)·scroll·unfolded·length·rows[(y,번호)]·height·issue_lanes·lanes. `_window`(watch_layout)가 render_frame(height=)과 실시간 화면을 같이 자른다.

**Why:** 고정 시험 충돌 두 개를 구현으로 풀었다. ① `test_view_done_state`는 끝난 주문의 쉬는 레인에 ▸가 없어야 함 → 끝난 주문 구역(done.py)이 이미 나열한 병합은 숨긴 줄로 세지 않는다. ② `test_view_sections`는 running_snapshot이 24줄에 FIRST·LAST 구역과 함께 들어가야 함 → `TIGHT = True` 구역(lanes)을 `tight=True`로 다시 그려 접힌 레인의 마지막 완료 줄·토글을 다른 구역보다 먼저 뺀다. `test_lane_view_d76`은 아무것도 안 하는 클릭이 state를 바꾸면 실패한다(그래서 store는 열 때만 저장).

**How to apply:** 실시간 화면은 `_frame(..., height=h, scrolls=True)`로 구역을 빼지 않고 STAYS만 옮긴다. 줄 검사 일반 시험은 `_frame(height=None)`으로 24줄 예산을 피한다. 실제 TTY 확인은 pty.fork로 자식에서 watch.run을 돌리고 키를 보내 1049h/l·OSC 8·위치 줄을 찾았다. 검증: `fa test tests/acceptance/test_acorn_interact.py tests/test_watch*.py tests/acceptance/test_squirrel_running.py tests/acceptance/test_view_sections.py tests/acceptance/test_view_done_state.py tests/acceptance/test_lane_view_d76.py` (335 passed와 같은 묶음). 화면 인수 63개 파일 비교에서 새 실패 0.

재시도 보완(2026-10-29): ① 결함 종류 "이슈 나열 누락" — status 레인 행의 `issue`는 대표 하나뿐이라 실행 없는 막힘·사람 기다림이 사라졌다. `collect_lanes`가 레인마다 `held`(number·title·state)를 쌓고 lanes가 작업자 뒤에 모두 그린다. 대기 줄은 레인의 phase를 빌리면 도토리가 잘못 찬다(phase None). ② "상태 판정 불일치" — 상자는 `status.local_group`(열린 PR=사람 기다림)+`issue_state`로 줄과 같은 규칙을 쓴다. ③ "레인 출처 충돌" — 상자 레인은 줄의 레인 → 최신 실행 lane → 라벨 순. ④ 오케스트레이터 지시: 끝난 주문은 접지 않음(▸ 없음), 위치 줄 전체 수는 높이 없는 24줄 화면 길이. 같은 묶음 비교는 기준 커밋을 `git archive <커밋>`으로 `.foragent/base`에 풀어 했다(새 실패 0).

2차 보완(2026-10-29): ① "주문 단위 예외가 레인 접기를 덮음" — 끝난 주문은 고정 시험 때문에 ▸를 못 쓰니, 완료 구역(done.py)의 이슈 줄에 ("acorn", n) 클릭 영역과 issue_lanes를 넣어 상자·f 접기에 닿게 하고, 펼친 쉬는 레인은 ▾ 접기를 보인다. ② "실행 끝남 ≠ 이슈 끝남" — 상자는 이슈가 closed일 때만 끝났어요(기록 없고 merged면 예외), 끝난 실행의 phase를 유지한다. 실행 없는 working 줄은 `snapshot.run_phases`의 단계를 쓴다. ③ "빈 시간 칸" — 모든 이슈 줄의 시간은 기록이 없으면 `—`(`elapsed_text(...) or "—"`). 회귀: `tests/test_watch_acorns.py` 23개, 고친 곳마다 되돌려 실패 확인.

3차 보완(2026-10-29): 끝난 주문 ▸ 문제는 리뷰가 세 번 반려했다. 해법은 주문 예외가 아니라 "접기 줄은 조작 수단": state 없는 화면(render_frame 상태 없음·`--once`)은 항상 tight라 마지막 완료 줄·토글을 그리지 않고, state 있는 화면은 주문과 상관없이 숨긴 줄이 있으면 ▸를 그린다. 고정 `test_view_done_state`(상태 없이 ▸ 금지)와 acorn_interact(state={}로 ▸ 요구)를 모두 만족한다. OSC 8은 curses refresh 뒤 쓰므로 `\x1b7`…`\x1b8`(DECSC/DECRC)로 감싸야 curses의 상대 이동(백스페이스 등)이 어긋나지 않는다. 기능 문구는 공유 messages 파일(watch.py·squirrel.py)을 고치지 말고 `messages/acorns.py`에 새 키(acorns_help·acorns_help_many·acorns_keys)로 둔다. 고정 레지스트리는 키 이름만 고정한다.

4차 보완(2026-10-29): ① "키 도움말은 스크롤 내용이 아님" — help 구역 줄은 style `help`(help.FOOTER)로 표시하고 `_window`가 그 줄을 content에서 뺀다. scroll_limit은 `state["content"]`(도움말 뺀 줄 수)로, 위치 줄 total은 고정 시험대로 frame 길이. `state["shown"]`이 화면 줄→frame 줄 지도(None=빈 줄·위치 줄)이고, 실시간 화면은 `pad=True`로 맞는 화면의 도움말을 맨 아래에 붙인다. 클릭·휠도 이 지도로 frame 줄을 찾는다. ② "선택은 이슈 번호가 아니라 줄 위치" — `highlighted_row`가 frame 줄 하나를 고르고 반전은 그 줄만(완료 목록의 같은 번호·줄바꿈된 둘째 줄은 강조 안 함). `_frame`은 새 그림마다 `shown`을 지운다. 주의: break-it용 백업 폴더(.foragent)가 없으면 cp가 실패해 변형이 쌓인다 — 먼저 `mkdir -p`.

5차 보완(2026-10-29): 3차의 "state 없으면 tight"는 리뷰가 "state에 따라 내용이 빠짐"으로 반려했다. 최종: 화면 내용은 state 유무와 무관(같은 줄), 24줄 초과일 때만 tight. 끝난 주문 ▸ 금지는 오케스트레이터 결정(+고정 test_view_done_state:65)이므로 `order_done and not is_open`이면 쉬는 레인 토글 없음으로 되돌리고 `.foragent/REBUTTAL.md`에 근거를 남겼다. 교훈: 고정 시험과 이슈 문구가 충돌하고 오케스트레이터가 정했으면, 리뷰가 다시 지적해도 우회 구현을 만들지 말고 처음부터 REBUTTAL을 쓴다. done_state 고정 시험은 큰 다람쥐로 24줄을 넘어 tight가 토글을 지우므로 이 규칙을 혼자 잡지 못한다 — 단위 시험이 잡는다.

최종 규칙(2026-10-29 스펙 갱신, 4·5차를 대체): 스크롤은 하나의 규칙. F = 높이 없는 render_frame(24줄 trimming·도움말 줄·불꽃 배치 포함, 큰 터미널에서도). 높이 h에서 F가 더 길면 `_window`가 F[scroll:scroll+h-1] + 위치 줄(N=len(F)), scroll은 0..len(F)-(h-1). 도움말 줄은 F의 보통 줄이라 같이 스크롤된다(4차의 help 스타일 제외·shown 지도·pad는 삭제). 높이 렌더 뒤 layout hits는 화면 줄(-scroll, h-1줄로 자름), 실시간 화면은 클릭 y를 그대로 넘긴다. 실시간 화면은 `_frame(..., layout, state)`(높이 인자 없음)+같은 `_window`. 끝난 주문 예외도 삭제: 숨긴 줄이 있으면 ▸(고정 done_state는 큰 다람쥐로 24줄 넘어 tight라 통과). 상자를 열면 scroll 0, 닫으면 `rows_scroll`로 복원. 교훈: 리뷰가 고정 시험과 스펙이 충돌한다고 반복하면 오케스트레이터가 스펙을 고친다 — 고친 스펙 문장을 그대로 구현하고 옛 우회 코드는 지운다.

공백 보완(2026-10-29): `watch._wrap`·`_line`은 `_clean`으로 연속 공백을 하나로 합친다. 원문을 보여 주는 이슈 상자는 `acorns._verbatim_wrap`(문자를 바꾸지 않고 마지막 공백 뒤나 폭에서 끊음, 이어 붙이면 원문)과 `[(piece, "")]`로 직접 Line을 만든다. `parse_body`의 goal은 `.strip()`되어 첫 줄 들여쓰기가 사라지므로 본문에서 "## Goal" 뒤 위치를 찾아 되살린다. 탭은 그 텍스트 자체 열 기준으로 펼친 뒤 "  "를 붙이고, C 범주 제어 문자는 뺀다. 레인 줄 제목 칸의 `_clean`은 한 줄 요약이라 REBUTTAL.

링크 캐시 보완(2026-10-29): OSC 8 링크를 "글자가 같으면 안 씀"으로 캐시했더니, 창 크기 변경 뒤 curses가 같은 글자를 지우고 다시 그려 링크가 사라졌다(리뷰가 PTY로 확인). curses는 크기 변경·자체 clear 때 같은 글자도 다시 그리므로 캐시를 지우고 refresh마다 쓴다. PTY 재현: TIOCSWINSZ로 크기를 바꾸고 SIGWINCH를 보낸 뒤 출력에서 `\x1b[2J` 이후에 링크가 있는지 본다.
