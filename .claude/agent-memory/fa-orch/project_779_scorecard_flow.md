---
name: project-779-scorecard-flow
description: D96 flow metrics in fa orch scorecard (#779) — module layout, binding events file, line limits hit, test pitfalls
metadata:
  type: project
---

- Flow metrics live in `orch_scorecard_flow.py` (`daily()` per bucket, `previous()` daily medians, `worse_names()`); `orch_scorecard.collect(store, days, lanes)` merges them. `worst()` skips `flow.NAMES` so fa brief keeps its line.
- Binding events go to `<state>/binding-events.jsonl`, NOT orch-events.jsonl: 56 tests in `tests/test_scheduler.py` assert exact orch journal kinds. **Why:** any new per-tick journal event breaks them. **How to apply:** new per-tick records get their own file.
- The tick writer is called from `orch_spawn.deciding()` normal exit (every acquired tick passes there); `scheduler.py` was exactly at its 250-line limit (test_scheduler_split), so no lines could be added there.
- SKILL.md of fa-orchestrator has no trailing newline: `wc -l` 149 = 150 splitlines lines (the limit). Rewrap with backtick-aware tokens; width up to ~144 is within existing lines.
- Idle minutes: segment sweep over change points, midpoint state; test-slot holding phase = `checking`; prepared run starts (`store.run_starts()`) occupy a slot until a run of that issue starts.
- Verified: Done-when 45, related 62 + scheduler/spawn/inbox 306 pass; skill-reading acceptance set identical before/after.
