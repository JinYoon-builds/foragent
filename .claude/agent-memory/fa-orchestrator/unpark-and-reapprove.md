---
name: unpark-and-reapprove
description: Parking an issue removes its approved label; unpark plus retry does not restore it, so re-approve by an order edit with a fresh preread
metadata:
  type: project
---

On 2026-11-03 ~17:25Z I parked #1475, #1491, #1791 and #1793. Parking removed `approved`. After the merge owner landed I ran unpark and retry, and the gate listed them as "ready", but nothing started for over an hour, because `approved` was missing.

**Why:** park takes the approval away by design; unpark only removes the waiting state.

**How to apply:** after `fa orch unpark N` and `fa orch retry N`, check the labels (`gh issue view N --json labels`). If `approved` is gone, re-approve all such issues in one order edit (`fa order edit N --json` → preread → `fa order publish --approve-numbers ...`). Related: [[unpark-blocked-needs-retry]] in the user memory.
