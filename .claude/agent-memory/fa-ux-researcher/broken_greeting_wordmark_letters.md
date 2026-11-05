---
name: broken-greeting-wordmark-letters
description: New-session statusline greeting's FORAGENT pixel word reads as EORAGENI (F and T have bottom bars)
metadata:
  type: project
---

Goal: see the product name in the new-session greeting (wide terminal).
Start: fa 0.1.0 9e0da018 (after #1894 / PR #1976), foragent repo.
Shortest repro: `echo "$j" | fa statusline --session-start` then `echo "$j" | COLUMNS=120 fa statusline`
with a fresh SessionStart JSON (session_id, cwd, workspace). Decode half-blocks to pixels (no PIL; write PNG with zlib).
Observed: letter 1 has a full bottom bar (same shape as E), letter 8 has a bottom bar (serif I) → reads "EORAGENI".
Confirmed by a second session 2026-11-06; re-reproduced and re-reported 2026-11-06 (fa orch request, saved as reported). Also: top half of each letter 1px narrower and shifted right.
Re-check: remove this entry once F and T render without bottom bars.
