---
name: broken-inspect-merge-queued
description: Broken flow — fa inspect calls merge-owner-queued PRs "awaits a human merge decision" (dashboard shows 질문)
metadata:
  type: project
---
Goal: the person checks `fa inspect` or the dashboard to see what needs them.
Start: a repo with `merge_owner: true` and a PR whose run ended merge-queued, with no open question.
Shortest repro: find the PR in `fa gate status` (pr-open list, and `merge-queued` in History). `fa orch facts` shows only live runs, so a finished merge-queued run can be missing there. Then `fa inspect` shows "— PR awaits a human merge decision", and `fa dashboard --once` shows `#N 질문`.
Observed 2026-11-05 (#1514 and #2147, then #1422 and #2147 in the rerun). Open #2078 covers the dashboard part. The inspect part was reported as r805 on 2026-11-05 because #2078 excludes inspect. Still broken on the 13:30Z and 22:40 local rechecks (#1422, #2147 shown as "awaits a human merge decision" and `#2147 질문`). r805 was accepted and became #2162 (parent) / #2163 (inspect fix); still broken at the 2026-11-05 evening recheck while #2163 was implementing. Do not report again while #2078 or #2163 is open.
**How to apply:** rerun the repro on the next visit. Delete this entry once both views show waiting (차례/대기).
