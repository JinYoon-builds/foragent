---
name: keep-approved-supply
description: Jin 11-04 rule — keep approved, startable issues at 2x the run slots; refill from Jin's priority list without waiting for PM requests
metadata:
  type: feedback
---

Keep approved, ready-to-run issues at least twice the run slots at all times (10-20 with 8 slots on the M4 Pro). When the count falls below that, do not wait for a PM request. Take the next decided issues in Jin's priority order: first-impression bundle, then allowed self-monitoring work, then other decided work. Merge path, runner, publish and new internal mechanisms wait until after the merge owner and the structure cleanup. Give several lane agents test drafts at once, and approve each one as soon as it is clean. Only Jin's share (taste, out of scope, hard to undo) goes to the box.

**Why:** Jin noticed on 2026-11-04 (r414) that about 80 issues were open but the approved queue ran dry several times that day. Two causes: the orchestrator waited for PM requests before preparing the next batch, and it did test drafts, red checks and preread one card at a time.

**How to apply:** check the approved queue on every wake (`fa gate status --json`: ready plus approved-waiting minus blocked or parked). Below 2x slots, start a parallel draft wave immediately. Batch publishes so main moves once per wave (see [[publish-moves-main-stale-heads]]). The PM adds a gate signal for this later (#1731 measurement), during the structure cleanup.

11-07 retro (orders #2554 #2564 #2566 #2568): four one-issue orders with prepared tests went 0 holds, 0 retries, about 2h35m from issue to close, all landing together in one batch merge at 13:11Z. Small prepared orders flow clean; their lead time is batch waiting, not work. Prefer this shape when refilling supply.

- 11-08 retrospective (#1649 #1862 #1870 #2677 #2703 #2832): orders stayed open for days because one child (#1653, decided 11-02) went redesign → park → unpark → re-approve; an order closes only with its slowest child. Small single-issue fixes (#2832) closed within hours. Keep size-l children split at order time so one slow child does not hold a whole order open; CI (33.5 min/run) is now the longest stage, measure it before the next capacity change.
