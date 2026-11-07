---
name: broken-issue-dashboard-merged
description: fa dashboard <N> --once shows merged issues as "병합 담당에게 넘겼어요", never merged (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: person asks whether one issue is done; PM shows `fa dashboard <N> --once`.
Repro: in foragent repo, `fa dashboard 1816 --once` (PR #2006 merged 2026-11-04, issue closed).
Observed: "마지막 실행 결과: 병합 담당에게 넘겼어요"; no merged/done word. Closed parent #1815 shows only "마지막 리뷰: 아직 없어요".
Reported to orch 2026-11-07 (order #1815 check); owner: orchestrator. Recheck: remove when the view says merged.
