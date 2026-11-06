---
name: main-red-check-after-merge
description: 11-06 main CI was red 50+ min unnoticed after #2267 (PM shortcut) met #1494; check main's ci after each merge and bisect locally
metadata:
  type: feedback
---

After each merge (and on each check), look at main's own `ci` runs: `gh run list --branch main --workflow ci --limit 3`. When main is red, every PR's CI fails, and holds look like each issue's own CI failure.

**Why:** on 2026-11-06 #2267 (PM shortcut, file_locks owner record truncates the locked file) and #1494 (send_lesson locks MEMORY.new.md itself) each passed alone. Together they broke two unit tests on main from 06:17Z. I first read #2209, #1821 and #2111 as separate CI failures. The same merge also broke `fa orch facts` (lock order, #2283).

**How to apply:** when two or more held issues fail the same test, check main first. Bisect on main's first-parent commits in a scratch worktree (one test file, nice 15), name the commit, write a failing acceptance test, order the fix alone at the front of the global queue (`fa orch queue --global`), then retry the held issues after it merges. Related: [[ci-timeout-loop-and-review-parser]], [[no-main-writes-during-train]].
