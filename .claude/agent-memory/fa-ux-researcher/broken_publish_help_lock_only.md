---
name: broken-publish-help-lock-only
description: After #2558, fa order publish --help still says statement issues always need reviewed tests; no lock-only exception
metadata:
  type: project
---
Goal: publish a lock-only fix of a published statement issue (orchestrator persona).
Repro: `fa order publish --help`. The statements paragraph says a real publish "refuses the issue until its lane has written tests and the configured reviewer has reviewed them". It has no lock-only or existing-lock exception. fa-order SKILL "Acceptance checks written" requires prepare `reviewed`, which a lock-only edit never reaches. Step 6 also says a change to "the tests" needs `fa order prepare` again.
Status: CONFIRMED 2026-11-08 (second session, 62d71a2c), re-reported via fa orch request. Runtime lock-only publish not walked; behaviour taken from #2558 merged acceptance test.
Recheck: remove this entry once the help names the lock-only path.
