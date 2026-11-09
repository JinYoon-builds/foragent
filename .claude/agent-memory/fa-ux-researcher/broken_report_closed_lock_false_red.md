---
name: broken-report-closed-lock-false-red
description: /fa report says a closed issue's locked tests "다시 실패해요" after orch judged it false alarm and order close says 완료
metadata:
  type: project
---
Goal: owner reads /fa report after an order finishes and trusts its status.
Start: main 3bb1c0bc, 2026-11-09 08:11Z, order #2876 closed "완료: #2877, #2878".
Repro: `fa brief --report --full | grep 2878` → "main에서 깨짐: #2878 test_… 다시 실패해요" (2 lines), though orch closed those alarms 07:43Z as false (passes on main 93aae973).
Status: confirmed by second session, reported 2026-11-09. Recheck after #2951 merges; remove when the lines are gone.
