---
name: too-big-split-at-order
description: "Too big for one session" hit 4 times on 3 issues (#1821 #2078 #2158) by 11-06; split at order time, not after the hold
metadata:
  type: feedback
---

When a lane answers "Too big for one session", split the issue or narrow its Goal through an order edit. Do not retry it.
On 11-06, this hold came 4 times on 3 issues (#1821, #2078, #2158). Each hold cost a full implement run (implementing went from 7.8 to 23.3 min per day).

**Why:** a retry does not shrink the work. The same hold comes back, and every rerun takes a run slot and a test slot.

**How to apply:** before you approve an issue on the merge path, count its Done when commands and Files. If it touches more than one state owner or more than about 4 files, split it in the plan. After the first "too big" hold, escalate only the split choice (through the PM). Related: [[prove-locks-can-pass]], [[ci-timeout-loop-and-review-parser]].
