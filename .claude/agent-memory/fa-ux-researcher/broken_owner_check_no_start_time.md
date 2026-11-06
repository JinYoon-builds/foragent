---
name: broken-owner-check-no-start-time
description: merge-owner.log "checks start" lines carry no time, so a running owner check cannot be told from a stuck one
metadata:
  type: project
---
Goal: Jin sees a batch sitting as 머지 and wants to know whether the merge owner's machine checks are still running or stuck.
Start: ~/.local/share/foragent/JinYoon-builds__foragent/merge-owner.log (after order #2102 / PR #2156).
Repro: `grep -n "merge owner checks" <log>`. Start lines look like `start: <branch|#N> (members) at <sha>`. No line in the file has a clock time, so a bare last start line gives no duration. Real elapsed values reach 18764.6s (#1422).
Status: confirmed and sent with fa orch request on 2026-11-06 (saved as 'reported', waiting for the confirmation step; no inbox id yet). Re-check after any fix; remove this entry once the start time is visible.
