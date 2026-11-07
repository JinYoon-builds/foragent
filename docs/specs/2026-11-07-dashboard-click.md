# Dashboard: short acorns, click windows, member cards

## Problem
Jin (2026-11-07, looking at the live dashboard after the 10-30 redesign returned): the layout reads much better, but
acorns that say "질문: 진님" or "대기: 오케스트레이터" are too long; it was unclear whether clicking an acorn still
explains the issue; the lane squirrels (each lane's agent) should open their own information; and the settings
window was hard to find. Jin: "이 레이아웃은 유지하되, 눌렀을 때 정보가 나오는 구조로".

Today a click on an acorn, a role name or a squirrel writes its explanation into the orchestrator's bubble at
the top of the view (src/foragent/watch_mouse.py, lane_explainers.py), far from what was clicked, and whether
mouse clicks reach the app inside the Orca terminal was never checked in a real terminal.

## Decision (Jin, 2026-11-07)
- Keep the current layout. Information appears when something is clicked.
- Acorns show the issue number and one state word only. Whose turn it is shows by color (red waits on a
  person, grey is machine work) and in the click window.
- An acorn opens an issue window; a squirrel opens a member card for its lane; the settings key is named in the
  footer and the settings button opens settings.
- No hardcoded person name anywhere on these surfaces (with order 2026-11-07-orch-owns-returns: person_name).

## Out of scope
- New screens, a new layout, animation changes.
- What the counts on the header line include (person-count-real) and how the person is named (person-name-config).
