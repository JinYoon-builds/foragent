---
name: broken-review-empty-diff
description: fa review on a branch with no changes vs base runs a silent ~4 min review, prints PASS; Ctrl-C prints a traceback but does not stop it
metadata:
  type: project
---
Goal: maintainer reviews a contributor branch with `fa review <branch>` (README flow).
Start: Foragent repo, branch equal to its merge base with main (e.g. `fa review main`).
Shortest repro: `fa review main`.
Observed (2026-11-06, fa 2a9201b6 and again on 9e0da018): no output for 221–241 s while a codex session runs, then `VERDICT: PASS` + Korean NOTE that the diff is empty, exit 0. SIGINT to the process group prints a KeyboardInterrupt traceback (thread pool join), but fa and its `codex exec` child keep running 48 s+ and must be killed by hand.
Expected: an immediate "nothing to review" line, no model session, and Ctrl-C stops the review and its children.
Status: confirmed by a second session 2026-11-06; reported via fa orch request.
