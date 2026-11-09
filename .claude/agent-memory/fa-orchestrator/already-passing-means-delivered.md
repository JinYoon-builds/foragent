---
name: already-passing-means-delivered
description: a spec question "Done when already passes on main" means an earlier merge delivered it — close it superseded at once, never ask a person (PM r1331, 11-09 #2673 blocked #2506)
metadata:
  type: feedback
---

#2673 (S6) stopped with "the Done when already passes on main; nothing to change". #2503 had removed the lock it tested.
PM r1331: this is not a person question; the orchestrator closes it right away (`fa orch close N --superseded-by M --why`).

**Why:** it blocked #2506 (a top priority) while waiting for an answer that has only one right value.
**How to apply:** verify the Done when passes on main and name the merge that made it pass (git log / bisect), then
close superseded by that issue; if the gate is busy, retry in a loop; then drop it from the global queue.

**11-09 addendum (PM r1333):** `fa orch close --superseded-by` closes as *not planned*, and a dependent issue then stops
("blocker closed as not planned"; #2506 behind #2673). Before closing, clear the dependents' blocked_by with an edit
(`"blocked_by": []` restated through `fa order edit`, prepare, publish --approve), or close as completed when a command allows it.
