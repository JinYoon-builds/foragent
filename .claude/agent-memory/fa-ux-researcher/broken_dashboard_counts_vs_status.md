---
name: broken-dashboard-counts-vs-status
description: Dashboard hero box running/waiting counts disagree with fa gate status (29 vs 16 waiting); held issues counted as waiting
metadata:
  type: project
---
Goal: read how many issues run and wait (#2321 promised it matches `fa gate status`).
Start: fa c728effa, foragent repo, 2026-11-07.
Repro: run `fa gate status | head -1` and `COLUMNS=120 fa dashboard --once | grep -E '작업하고|기다려요'` back to back.
Observed: status "일하는 중 4 · 확인할 일 8 · 기다리는 일 16"; dashboard "이슈 3개를 작업" / "30개는 순서를 기다려요". The 29/30 includes 보류 and 차례 chips.
Status: CONFIRMED by second session 2026-11-07 03:11Z (waiting 30 vs 16; running matched 3=3), re-reported. Remove after the fix is verified.
