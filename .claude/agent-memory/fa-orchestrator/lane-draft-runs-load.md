---
name: lane-draft-runs-load
description: Lane-agent test drafts run pytest outside FA test slots; tell each one file at a time, nice 15, target files only
metadata:
  type: feedback
---

Every lane-agent draft prompt must say: one test file at a time, `nice -n 15`, the new locks and the touched files only; no multi-file batches.

**Why:** on 2026-11-03 five draft agents ran pytest at once outside the slots (firstrun ran 23 files). Load reached 1.52 per core, the gate cut test slots to 1, and lanes waited. The PM asked for this.

**How to apply:** put the line in the Agent prompt from the start; do not run two draft agents' big checks together. See [[prove-locks-can-pass]].
