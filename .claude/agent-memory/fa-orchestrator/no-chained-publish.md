---
name: no-chained-publish
description: Do not chain unpark and retry on one line after fa order publish; a refused publish still runs them
metadata:
  type: feedback
---

Run `fa order publish --approve` alone, confirm the `approved` label, then run `fa orch unpark` and `fa orch retry`. Before publishing, confirm the preread ran for the exact plan.

**Why:** on 2026-11-01 a publish for #1509 was refused for lack of a preread. The same command line went on to unpark and retry, and my session note claimed re-approval. The issue had also lost its `approved` label. I re-parked it before a run started.

**How to apply:** an order edit needs a fresh preread even when only a locked test changes. Plan about 10 minutes for it before the next merge batch. Report re-approval only after reading the label.

`fa orch park` removes `approved` (orch_park.py:95) and `unpark` does not restore it, so after an unpark an issue sits `ready` and never starts until it is re-approved through an order edit with a preread. #1158 sat that way for 1.5 hours on 2026-11-01. A park also writes `## Waiting` to the body, which invalidates a preread already running.

