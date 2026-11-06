---
name: broken-closed-parent-no-card
description: Closed order parents still have no board card after #1577 (card repair); some new closes get one, others never do (confirmed 2026-11-06)
metadata:
  type: project
---

Goal: owner sees every finished order as Done on the "Foragent: foragent" Project board.
Start: gate running, #1577 merged 2026-11-05T23:37Z.
Shortest repro: `gh issue list --state closed --limit 400 --search '"foragent:plan id=" in:body' --json number,projectItems --jq '[.[]|select(.projectItems|length==0)]|length'`. This gave 75 on 2026-11-06 12:10Z. `gh issue view 2216 --json projectItems` gave [] (closed 4 min after the merge). Compare #2324, closed in the same way, which got a Done card 40 min later.
Observed: an inconsistent board; old and some new closed parents never get a card. Confirmed by a second session. Reported to the orchestrator on 2026-11-06; re-reproduced and re-reported 12:14Z (still 75).
Retest: when the count reaches 0, delete this entry.
