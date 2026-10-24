---
name: issue-365-machine-yml
description: #365 machine.yml is the single source for shared budget files; whole-file fallback to repo values keeps older locked tests passing
metadata:
  type: project
---

`src/foragent/machine_budget.py` reads `capacity.machine_root()/machine.yml` (keys test_slots, test_slots_floor, parallel_floor, max_parallel).

**Why:** locked `test_test_slots`/`test_adaptive_budgets` have no machine.yml and expect a repo's `test_slots: 3` to drive test-slots.json, while `test_machine_budget_config` needs machine values to win. So: file present → machine values + built-in defaults for missing keys; file absent → the ticking repo's own values (today's behaviour). Machine max_parallel caps capacity.json; a repo max_parallel stays own-admission only.

**How to apply:** tick and `_machine_budget` call `machine_budget.values(ctx.config)`; `effective_test_slots` caps by `configured()` only when the file exists; `gate status --json` adds `machine_budget = sources(config)`. scheduler.py facade sits at exactly 250 lines (split-test limit).
- Review fixes: a tick that starts nothing (no ready work, `start_runs` off, outside run hours) still calls `_machine_budget` so capacity.json follows machine.yml and pressure; machine `max_parallel` caps after the decrease cooldown. Reviewer's "no file must use fixed defaults" conflicts with locked tests (7 fail) → REBUTTAL.
- Reviewer rejected the no-file fallback rebuttal twice; locked Done-when tests need it, so .foragent/QUESTION.md asks the owner (keep / edit locked tests / auto-seed machine.yml).
