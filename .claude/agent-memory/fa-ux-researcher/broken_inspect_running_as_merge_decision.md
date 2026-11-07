---
name: broken-inspect-running-as-merge-decision
description: Broken flow — fa inspect lists a running (전체 시험 중) issue's open PR as "awaits a human merge decision"
metadata:
  type: project
---
Goal: the owner opens `fa inspect` to see what needs them.
Start: merge_owner: true; an issue in `fa gate status` as `일하는 중 · ... · 전체 시험 중` with its PR open (labels approved,running).
Shortest repro: compare `fa gate status` (running), `fa dashboard --once` (`#N 전체`) and `fa inspect` (`[review] ... — PR awaits a human merge decision`).
Observed 2026-11-06 15:10Z and 15:14Z on #2308 / PR #2313. Second session reproduced it 2026-11-06 15:16Z (same steps); reported as confirmed via fa orch request (status reported).
The older merge-queued variant (#2163) is fixed: pr-open PRs no longer appear in inspect.
**How to apply:** rerun on the next visit; delete once a running issue is no longer shown as a merge decision.
