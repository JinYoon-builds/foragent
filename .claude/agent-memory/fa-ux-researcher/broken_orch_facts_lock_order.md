---
name: broken-orch-facts-lock-order
description: fa orch facts crashes with "Lock order inversion" in the foragent repo when a foragent-sandbox store exists (#2283)
metadata:
  type: project
---
Goal: read orchestrator facts (and check the #1787 plan-pause label).
Start: Jin's Mac mini, /Users/apple/Dev/foragent, a JinYoon-builds__foragent-sandbox store also in ~/.local/share/foragent.
Shortest repro: `fa orch facts` (or `--json`) → exit 1, traceback "RuntimeError: Lock order inversion: ...foragent-sandbox/state.lock after (capacity.lock, ...foragent/state.lock)".
Seen and reconfirmed by a second session 2026-11-06 on 0.1.0 6108b37e. Tracked as open issue #2283; reported in the order #1785 check. Blocks verifying the #1787 plan-pause label.
**How to apply:** after #2283 merges, rerun; if facts works, check a plan-paused lane is not called quota, then delete this entry.
