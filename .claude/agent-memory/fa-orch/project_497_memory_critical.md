---
name: issue-497-memory-critical
description: #497 memory no longer sizes budgets; critical kernel level only holds starts/test slots; floors retired via config.IGNORED_KEYS
metadata:
  type: project
---

#497: `machine_load()` returns the raw kernel level (swap-ins are only a recorded fact). Callers pass `pressure="normal"` to `next_parallel`/`next_test_slots` (the pure functions keep pressure semantics because `test_capacity_aimd` locks them). Critical hold lives in `scheduler_start._start_ready`: the loop runs every start check (overlap, approval, integrity, prerequisites) and the row is written just before `_reserve_slot` for the first issue that passed them all, then it returns. Review rejected a pre-loop capacity/lane check because overlap-held work would log a hold that memory did not cause and in the facade, which skips the baseline prebuild. `hold_test_slot` re-reads `kernel_pressure()` per poll; TEST_SLOT_ENV held still yields at once.

**Why:** floors and memory AIMD made budgets collapse on warn/swap-ins.

**How to apply:** retired YAML keys go in `config.IGNORED_KEYS` (dropped in `_overlay`, so rewrites of the global file keep them); machine_budget skips them in unknown_keys. scheduler.py facade is at 247/250 lines.
