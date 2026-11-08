---
name: broken-hold-answered-still-awaiting
description: Repeated-class review hold shows "awaiting PM decision" after the PM question is answered; inbox shows raw marker, no question id/options (CONFIRMED 2026-11-07)
metadata:
  type: project
---

Goal: owner wants to know why a held issue is stopped and what comes next.
Start: foragent repo after #2418/PR #2422 (2026-11-07). Issues #2312 and #2215 were held for review-defects.
Shortest repro: `fa dashboard 2312 --once`, then `fa inspect` (look at #2312 and #2215), then `fa orch questions --all --json` (q44, q36).
Observed: the dashboard and inbox say "Two failed rounds of the same class; awaiting PM decision". The inbox item is filed as [tech-failure] and shows the raw `<!-- foragent:blocked reason=review-defects -->` marker. It names no question id, no options and no next step. The matching questions q44 and q36 are already closed with answers. `fa orch questions` prints "No open questions".
Confirmed by a second session 2026-11-07 01:13Z and reported to orch as CONFIRMED. q44 answer is "PM splits #2312", so the next step there is a split, not a retry. Recheck after a fix merges or #2215/#2312 move.
