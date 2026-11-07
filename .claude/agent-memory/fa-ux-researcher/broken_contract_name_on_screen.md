---
name: broken-contract-name-on-screen
description: docs/design/CONTRACT.md rule 5 (no person name) contradicts the dashboard's 진님 from #1160 (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: a view-lane reviewer follows docs/design/CONTRACT.md (from #2316) when judging the dashboard.
Start: main after order #2314.
Shortest repro: read CONTRACT.md rules 4-5, then `COLUMNS=100 fa dashboard --once` in the foragent repo.
Observed: 진님 shows in the header, the bubble and "질문: 진님" chips. Closed #1160 and its locked test test_question_recipient.py require it, so the two rules conflict.
Reported 2026-11-07 via fa orch request (re-confirmed and re-sent the same day; earlier send not found by grep in inbox) (A: contract exception line, B: remove the name). Waiting on Jin's answer through the orchestrator. Remove this entry when the contract and the screen agree.
