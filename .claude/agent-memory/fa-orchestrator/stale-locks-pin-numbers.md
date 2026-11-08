---
name: stale-locks-pin-numbers
description: 11-09 five issues stopped on old locks pinning a count, time or exact text; prove corrections on the issue branch, and the #2841 route needed #2868 first
metadata:
  type: project
---

Old acceptance locks that pin a number, a time window or exact wording stopped #2734 (exact error text), #2840 (3 h
window), #2352 (publishes-anyway), #2822 (exactly 3 rows / 6-row bubble helper) on 11-09. Each correction keeps the test's
purpose and drops only the pinned means (D98).

**Why:** the #2841 lock-correction route refused every prepared issue: its restate check read the generated
"## Preparation notes" + plan-child marker as authored text (fixed by #2868). Until then only the PM shortcut lands them.
**How to apply:** prove each correction yourself in a detached worktree of the issue branch (old fails, new passes) and
on main (green or not decides: ahead vs in the issue PR). Shared helpers (bubble readers) count as locks too: grep importers
and run them on main before and after. An issue branch still on old behaviour cannot prove anything: retry the
implementation first with the decision in the hint, then correct.
