---
name: init-hook-not-in-plan
description: fa init --dry-run never mentions the memory-write reminder hook (#2928/PR #2929) it should add to Claude settings; README says only Bash(fa:*) changes
metadata:
  type: project
---

Goal: before re-running fa init, see what it will change in Claude Code settings.
Start: fa 0.1.0 (7abb1cd7, includes PR #2929), foragent repo or any fresh temp repo.
Repro: `fa init --dry-run | grep -iE 'hook|settings'` → no hook or settings file listed.
Observed: README says only the one `Bash(fa:*)` rule changes; the 11-04 memory-decision-guard.py is still the only memory hook and prints nothing on a memory write.
Real fa init not run (would commit/push a dirty tree; isolated HOME fails "No logged-in coding tool found"). Reported 2026-11-09; dry-run part reproduced by second session and re-reported same day (real-init part still unverified).
