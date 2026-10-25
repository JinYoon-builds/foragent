---
name: issue-365-machine-yml
description: #365 machine.yml is the only source for shared budget files (strict reading); invalid-key handling, ceiling clamp, status lines
metadata:
  type: project
---

`src/foragent/machine_budget.py` `read()` loads `capacity.machine_root()/machine.yml` on every call (no cache) and never raises. Defaults come from `Config` dataclass field defaults. Per key: valid = `type(v) is int and v >= 1` (bools, null, str, float invalid → default, source `invalid`); a floor above its ceiling marks only the floor invalid; bad YAML / non-mapping marks all keys invalid; unknown keys are ignored and listed in `report()["unknown_keys"]` only when non-empty (the locked status test compares the 4-key dict exactly).

**Why:** owner answered the spec question 2026-10-25 with the strict reading: without machine.yml the shared files use fixed defaults; repo `.foragent.yml` never sizes them. Older locked tests were changed to write machine.yml via `test_test_slots.machine_yml()`.

**How to apply:**
- Shared ceilings (test_slots, max_parallel) clamp a stored value above them at once, alone in that tick, one history row, decreased_at untouched (`update_test_slots`, `_machine_budget`).
- A tick that starts nothing still calls `_machine_budget` so capacity.json follows machine.yml and pressure.
- Repo `test_slots`/`max_parallel` only limit own admission (`effective_test_slots`, `_has_slot`). Repo floors are ignored; `gate status` prints a line when `.foragent.yml` contains a floor key (`status["repo_floors_ignored"]`). Human lines live in `messages/machine_budget.py`.
- Unit tests that set floors write machine.yml (`write_machine_yml` helpers in test_capacity/test_scheduler).
