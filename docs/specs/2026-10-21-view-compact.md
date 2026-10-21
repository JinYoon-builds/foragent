# Compact lane view: fold idle lanes

Follow-up to D41 (lane conveyor TUI), from Jin's use on 2026-10-21.

### view-compact — fold idle lanes and keep table rows on one line (size s)
Goal: In `fa gate watch` (live view and `--once`, same layout):
(1) Lanes with no work (no current issue, nothing queued, nothing blocked or waiting for a person) fold into ONE road line, e.g. `쉬는 레인: 3 🧩 Docs · 6 🧰 공통 ✓2` (en: `idle lanes: ...`). Each item keeps its lane number and emoji, plus `✓N` when the lane finished something in this view. Lanes with work keep the full road rows. When a lane gets work it unfolds automatically.
(2) Number keys keep working for every lane, folded or not (footer still `1–N`).
(3) The "now doing" table lists only lanes with work, exactly one line per lane: text that does not fit is cut with `…` instead of wrapping. When no lane has work, show one line saying all lanes are idle.
Jin asked for this after the right-side Orca pane showed only two lanes: idle lanes took three rows each.
Out of scope: Colours, animation, the orchestrator box, the detail view.
Test checks: tests/acceptance/test_view_compact.py
