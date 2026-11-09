---
name: broken-lane-row-baseline-as-next
description: Dashboard lane row/bubble count issues in the baseline step ("기준 재는 중") as next/waiting, while fa status and fa dashboard <n> say running (seen once 2026-11-09)
metadata:
  type: project
---

Goal: see how many issues run, from `fa dashboard --once` in /Users/apple/Dev/foragent.
Start: a newly started issue is in the baseline step (`fa dashboard <n> --once` says "기준 재는 중"; label running).
Shortest repro: run `fa dashboard --once` and `fa status` together during that step.
Observed 2026-11-09 03:37Z: the bubble said "이슈 3개를 작업 · 12개는 순서를 기다려요". #2673 and #2675 were plain chips under "다음". fa status said "일하는 중 5". At 03:40Z, after the baseline step ended, the bubble said 5 and the row showed both issues as 작성.
Status: reported 2026-11-09 after order #2903 (#2905). It waits for a second-session confirmation; there is no inbox id yet. Responsible: orchestrator. Next condition: confirm at the next issue start, then remove this entry when the row shows baseline-step issues as working.
