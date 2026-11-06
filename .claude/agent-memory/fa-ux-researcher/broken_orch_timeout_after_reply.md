---
name: broken-orch-timeout-after-reply
description: needs-human "no response within 30 minutes" posted although the orchestrator replied (CONFIRMED 2026-11-07); CI-wait hold under D89 still unconfirmed
metadata:
  type: project
---
Goal: the owner reads an issue to learn why a green PR did not merge.
B (CONFIRMED 2026-11-07): #2357 blocked 17:53:57Z (2026-11-06), orch comment 18:00:58Z, needs-human 18:26:05Z says "no response to this blocker within 30 minutes"; blocked+needs-human still on next day, PR #2365 open, all checks green. Same on #2356 (orch 17:31:09Z, claim 18:02:08Z).
A (UNCONFIRMED, dropped from report): PR #2365 held "CI ran past the wait limit" while test (3) ran; cannot tell from outside if #2383 (D89, merged 17:41:39Z) was live.
**How to apply:** recheck the next wait-limit hold after #2383 is surely deployed, and the next orchestrator-timeout on an issue with an orch reply; remove each part once it no longer happens.
