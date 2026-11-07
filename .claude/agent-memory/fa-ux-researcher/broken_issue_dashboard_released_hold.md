---
name: broken-issue-dashboard-released-hold
description: fa dashboard <N> --once shows a released, queued hold (#2482) as "사람 확인을 기다리며 멈췄어요"; main view shows 대기 (CONFIRMED 2026-11-07)
metadata:
  type: project
---
Goal: the owner checks whether his approved edit unblocked a held issue.
Start: foragent repo, fa 70caa1b6 (contains #2599/PR #2606). #2482 was blocked 08:36Z, and its edit was approved 10:46Z on 2026-11-07.
Repro: `COLUMNS=100 fa dashboard 2482 --once`, then compare with `fa dashboard --once` (lane 🧰 공통).
Observed: the single-issue view says "마지막 실행 결과: 사람 확인을 기다리며 멈췄어요" and shows the old defects. It does not mention the approval. The main view shows "#2482 대기". The `blocked` label stays on. Same root as [[broken-issue-dashboard-merged]]: the view shows the last run outcome, not the current state.
Status: CONFIRMED by a second session 2026-11-07. Earlier report not found in inbox; reproduced again and reported via fa orch request 2026-11-08 (owner: orchestrator). Recheck: rerun the repro when a fix merges or #2482 starts running. Remove this entry when the view shows the current state.
