---
name: broken-inspect-merge-queued
description: Broken flow — fa inspect calls merge-owner-queued PRs "awaits a human merge decision" (dashboard shows 질문)
metadata:
  type: project
---
Goal: person checks `fa inspect` / dashboard to see what needs them.
Start: repo with `merge_owner: true` and a run whose outcome is merge-queued, no open question.
Shortest repro: `fa orch facts | grep merge-queued` → `fa inspect` shows "— PR awaits a human merge decision"; `fa dashboard --once` shows `#N 질문`.
Observed 2026-11-05 (#1514, #2147). Dashboard part is in open #2078; inspect part reported via `fa orch request` the same day (#2078 excludes inspect).
**How to apply:** re-run the repro next visit; delete this entry once both show waiting (차례/대기).
