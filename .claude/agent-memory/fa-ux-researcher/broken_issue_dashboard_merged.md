---
name: broken-issue-dashboard-merged
description: fa dashboard <N> --once shows merged issues as "병합 담당에게 넘겼어요", never merged (confirmed; rechecked 2026-11-08 after #2208)
metadata:
  type: project
---
Goal: person asks whether one issue is done; PM shows `fa dashboard <N> --once`.
Repro: in foragent repo, `fa dashboard 1816 --once` (PR #2006 merged 2026-11-04, issue closed).
Observed: "마지막 실행 결과: 병합 담당에게 넘겼어요"; no merged/done word. Closed parent #1815 shows only "마지막 리뷰: 아직 없어요".
Reported to orch as r923 (accepted 2026-11-06); owner: orchestrator. Rechecked 2026-11-07 (order #1822): `fa dashboard 1821 --once` still shows it. Rechecked 2026-11-07 after order #2120 (b2bf7363): 1816 and 1821 still show it. Rechecked 2026-11-07 after order #2236 (b2bf7363, CI line gone): 1816/1821 unchanged; closed order parent #2236 shows only "마지막 리뷰: 아직 없어요". Rechecked 2026-11-07 after order #2490: `fa dashboard 2492 --once` (PR #2514 merged) still says 넘겼어요; closed parent #2490 and replaced child #2491 show only "마지막 리뷰: 아직 없어요"; `fa dashboard 2494` says Foragent does not know the replacement. Rechecked 2026-11-07 14:18Z after orders #2549/#2562/#2570/#2573 (22bd8309): `fa dashboard 2563 --once` (PR #2582 merged 13:56Z) still says 넘겼어요; plain `fa orch facts` Runs list also shows "#2563 merge-queued" (run outcome, same root). Rechecked 2026-11-08 after orders #2208/#2439: `fa dashboard 2209 --once` (PR #2605 merged 2026-11-07 15:34Z) still says 넘겼어요; closed parents #2208/#2439 show only "마지막 리뷰: 아직 없어요". Remove when the view says merged.
