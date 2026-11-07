---
name: broken-design-holds-unchanged
description: After #2501 merged, fa orch facts repeated_design_holds still lists repeat-check holds (#2458:2 etc.); false architect alarm can recur
metadata:
  type: project
---
Goal: the architect alarm counts only holds where a reviewer ran (order #2500 / #2501, PR #2513).
Start: installed fa f2c89a48 (contains the 85910ef1 #2501 merge), 2026-11-07 ~08:00Z.
Repro: `fa orch facts --json` → kind_numbers.repeated_design_holds.
Observed: {#2209:2, #2215:3, #2312:2, #2454:2, #2458:2}. This is unchanged from r1011. Architect r1012 says #2458 has 1 reviewed hold, so the expected value is {}.
Status: CONFIRMED; reproduced again 2026-11-07 08:09Z, reported via fa orch request (orchestrator owns it, next: architect replay r969/r1012).
Recheck: rerun the command. Remove this entry when the old repeat-check holds drop out of the value.
