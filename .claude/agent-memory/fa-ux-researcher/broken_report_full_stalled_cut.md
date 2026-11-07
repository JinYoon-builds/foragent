---
name: broken-report-full-stalled-cut
description: fa brief --report --full cuts stalled lines with raw markdown; decision list double-counts #1957 and #2458 sits in both stalled and decisions
metadata:
  type: project
---

Goal: read the full /fa report and see why each item stopped and what is his to decide.
Start: foragent checkout with stalled (orchestrator no response) items and an open human hold.
Repro: `fa brief --report --full`.
Observed 2026-11-07 05:02Z on fa 764a6f50:
- Stalled lines cut mid-blocker with raw markdown ("— **Acc…") although --full promises full detail. Confirmed (second session).
- "결정 기다리는 일 5개", but #1957 is listed twice (once without options) and #2458 is in both 멈춘 일 and 정해 주실 일. Seen once, unconfirmed.
Status: sent via fa orch request 2026-11-07. Remove once full lines and a deduplicated count show. Related: [[broken-report-full-after-short]].
