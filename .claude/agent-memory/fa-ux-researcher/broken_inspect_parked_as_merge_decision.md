---
name: broken-inspect-parked-as-merge-decision
description: fa inspect lists parked (보류) issues as "PR awaits a human merge decision" (confirmed 2026-11-06)
metadata:
  type: project
---
Goal: the owner uses fa inspect to see which PRs need a merge decision now.
Starting conditions: the issue is parked (its latest comment is a parked comment, the dashboard chip shows 보류) and its PR is still open.
Shortest reproduction: `fa inspect` lists `[review] ... #1004 ... — PR awaits a human merge decision`. Also #1296 and #1653. `fa dashboard --once` shows all three as 보류.
Observed failure (fa 163033ba, second session confirmed): parked issues are 3 of the 5 Review items. #1004 was parked by Jin's "stop trying to merge". Reported on 2026-11-06. Remove this entry when inspect has no [review] item whose chip is 보류. Related: [[broken-inspect-running-as-merge-decision]].
