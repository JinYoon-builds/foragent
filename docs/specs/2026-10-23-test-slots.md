# Test slots, separate from worker slots

Evidence (2026-10-23): AI thinking runs remotely; pytest and venvs load the machine. With 4 concurrent test runs the same tests ran 4x slower, and the loss is a cliff: a run passing a fixed limit (baseline 900 s, run budget 90 min) throws away its work.

- `test_slots` (default 2) caps concurrent test commands machine-wide, separately from `max_parallel`. The runner's acceptance baseline, Done-when checks and related unit tests each hold a slot while running.
- Time waiting for a slot goes to `waited_seconds` and is not charged to the baseline limit or the run budget.
- `fa test -- <pytest args>` runs pytest inside a slot and passes output and exit code through. The implementer prompt and generated lane agent files name `fa test` as the way to run tests: a tool, not a prohibition.
- The effective slot count follows the #106 AIMD signals: one step down under high load (never below 1), back up to the configured ceiling with headroom; `fa gate status --json` reports it as `test_slots`.
- With tests bounded, `max_parallel` can rise by quota and memory; the ratio comes from #224/#225 phase times (AI vs test time) and load.
