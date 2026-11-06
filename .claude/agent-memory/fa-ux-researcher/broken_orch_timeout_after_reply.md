---
name: broken-orch-timeout-after-reply
description: needs-human "no response within 30 minutes" posted although the orchestrator replied; plus a green PR held "CI ran past the wait limit" 12 min after D89 (#2383) merged (UNCONFIRMED)
metadata:
  type: project
---
Goal: the owner reads an issue to learn why a green PR did not merge.
B (orch timeout): #2357 blocked 17:53:57Z, orch comment 18:00:58Z, needs-human 18:26:05Z says "no response to this blocker within 30 minutes"; labels blocked+needs-human stay. Same on #2356 (orch 17:31:09Z, claim 18:02:08Z). Orch reply "fix ciskip pending" pointed at #2343, closed as replaced.
A (CI wait): PR #2365 held "CI ran past the wait limit" 17:53:57Z while test (3) still ran (done 17:54:13Z); #2383 merged 17:41:39Z. Unknown whether the gate ran the new code yet.
Reported 2026-11-06 via fa orch request (reported, unconfirmed).
**How to apply:** recheck the next wait-limit hold after the gate surely runs #2383, and the next orchestrator-timeout on an issue with an orch reply; remove each part once it no longer happens.
