---
name: broken-d49-backlog-missing
description: D49 kept-backlog decision (10-31) has no open issue; #1034/#1035 are NOT_PLANNED while fa orch inbox shows "D49: in progress"
metadata:
  type: project
---
Goal: the owner finds where the D49 backlog that Jin kept on 2026-10-31 (order #1241) lives.
Repro: `gh issue list --state open --search "D49 in:title"` returns nothing; #1034/#1035 were closed NOT_PLANNED 2026-10-30. `fa orch inbox | grep D49` still shows "D49: in progress (... #1034, #1035 ...)". Rechecked 2026-11-07, unchanged.
Reported to the orchestrator (addendum to r874/r877); waiting on its D49 decision. Do not re-report.
**How to apply:** after the orchestrator decides on D49, recheck the open D49 count; remove this entry once the records agree.
