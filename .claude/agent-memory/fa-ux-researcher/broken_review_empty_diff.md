---
name: broken-review-empty-diff
description: fa review on a branch with no changes vs base runs a silent ~4 min xhigh review, prints PASS; Ctrl-C does not stop it
metadata:
  type: project
---
Goal: maintainer reviews a contributor branch with `fa review <branch>` (README flow).
Start: Foragent repo, branch equal to its merge base with main (e.g. `fa review main`).
Shortest repro: `fa review main`.
Observed (2026-11-06, fa 2a9201b6): no output for 241 s while a codex xhigh session runs, then `VERDICT: PASS` + note that the diff is empty, exit 0. SIGINT to the foreground group leaves fa running 15 s+; after SIGTERM, the codex child can be orphaned (ppid 1).
Expected: an immediate "nothing to review" line, no model session, and Ctrl-C stops the review and its children.
Reported via fa orch request on 2026-11-06 (unconfirmed by second session).
