---
name: inspect-prepare-real-state
description: Fake open questions in the real inbox can come from fa inspect prepare running old PRs' Try it commands, not from tests; how to trace a stray state write
metadata:
  type: project
---

On 2026-11-03T15:52:39Z, open questions q5 and q6 (PR #50 merge, order card add) appeared in the real inbox. The cause was `fa inspect prepare`. It re-prepared 87 old PRs and ran #970's "Try it" CLI commands (`fa orch ask ...`) against the real ~/.local/share/foragent state. pytest already isolates HOME (tests/conftest.py, #655).

**Why:** the PM first suspected a test. Searching only tests/ found one of the two texts and missed the source.

**How to apply:** for an unexplained record in state, (1) find its exact timestamp in orch-events.jsonl, (2) `find <state> -newermt` around that second to see what else was written (inspect/prepared/*.json gave it away), (3) grep agent transcripts under the Orca profile projects dir for the exact text. A fix issue was drafted on 2026-11-04 (key inspect-try-isolation). Until it merges, close such questions as fixture data.
