---
name: broken-report-full-after-short
description: fa brief --report --full run right after --report shows 머지 0 / 끝난 일 없어요 although the short report counted merges
metadata:
  type: project
---

Goal: read the three-line /fa report, then see the details of what it counted.
Start: foragent checkout; at least one issue closed since the last report (머지 counts closed issues, not PRs).
Repro: `fa brief --report` then right away `fa brief --report --full` (each run moves the shared report time).
Observed 2026-11-07 ~04:10Z on fa 764a6f50: short "지난 보고 뒤 머지 34개"; full "머지 0개" + "지난 보고 뒤 끝난 일은 없어요". No merge list anywhere (`fa brief`, `--json` lack one).
Status: reproduced 04:10Z; recheck 05:02Z could not repeat (short already said 머지 0). Waits for a repeat after the next issue closes. Remove once --full shows the window the short report counted.
