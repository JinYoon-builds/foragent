---
name: broken-retired-main-write-invisible
description: After #2760, a retired main-write request (106) is not shown in fa gate status; help promises the --why reason is shown there
metadata:
  type: project
---
Goal: see the outcome and reason of a retired main-write request (#2760 Goal: status shows it with its reason).
Start: /Users/apple/Dev/foragent. The queue moved past blocked request 106 (2026-11-08).
Repro: run `fa gate status` and `fa gate status --json`. main_writes lists only pending seq 191. There is no retired entry and no reason. `fa orch main-write retire --help` says the reason is "shown in status".
Status: confirmed by a second session and reported 2026-11-08 ~11:15Z. Check again after the orchestrator acts. Remove this entry once status shows retired requests.
