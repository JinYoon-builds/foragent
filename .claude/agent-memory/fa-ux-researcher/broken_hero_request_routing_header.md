---
name: broken-hero-request-routing-header
description: Dashboard hero box shows "기획 세션 요청: Decision: orchestrator", the routing header of an architect request
metadata:
  type: project
---
Goal: learn from the hero box what the planning session asks.
Start: fa c728effa, an architect request (e.g. r992) is open, 2026-11-07.
Repro: `COLUMNS=120 fa dashboard --once | grep '기획 세션 요청'`.
Observed: "기획 세션 요청: Decision: orchestrator". The real subject is on the next line of the request, and the sender is architect, not the planning session.
Status: CONFIRMED by second session 2026-11-07 03:11Z, re-reported. Remove after the fix is verified.
