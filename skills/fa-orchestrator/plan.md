# Lane plan

The plan (`<state>/plan.json`) tells the gate, for a while, which lanes to hold, in which order to
start their issues and how many runs each lane may have. It is a preference: blockers, the machine
budget, the file-overlap cap and exclusive rules still win. A plan never starts a run they would
refuse, and a queue never unpauses a lane.

Always pass `--by orchestrator`. Without it the command records the change as a person's.

| Need | Command |
| --- | --- |
| See the plan | `fa orch plan` (or `--json`) |
| Hold a lane | `fa orch pause LANE --until ISO --why "<reason>" --by orchestrator` |
| Release it early | `fa orch resume LANE --by orchestrator` |
| Order a lane's issues | `fa orch queue LANE N N ... --by orchestrator` (`--clear` removes it) |
| Cap a lane's runs | `fa orch plan --width LANE=N --by orchestrator` (`LANE=off` clears; repeatable) |
| End the whole plan | `fa orch plan --expires ISO --by orchestrator` |
| Let N runs share one file | `fa orch plan --overlap PATH=N --why "<evidence>" --by orchestrator [--expires ISO]` (`PATH=off` clears; repeatable) |
| Choose one issue's executor and model | `fa orch assign N --executor NAME --model NAME [--until ISO] --why "<reason>" --by orchestrator` |
| Return it to ordinary selection | `fa orch assign N --clear --why "<reason>" --by orchestrator` |
| Record Jin's goal (only when Jin states it) | `fa orch goal "TEXT" --deadline ISO --why "<Jin's words>" --by human` |

- A pause needs both `--until` and `--why`; it lapses by itself when `--until` passes.
- Times are ISO; a time without a timezone is local time. Past times are refused.
- When the plan expires, everything in it lapses and lanes follow the defaults. The next edit starts a
  fresh plan with no expiry.
- Without a width a lane has no fixed cap: free slots fill with ready issues whose files do not
  overlap running ones. Use a width only to hold a lane back; use a pause to stop it.
- A file limit replaces `overlap_per_file` for that one file; other files keep it. Choose N from the
  file's rows in `fa orch facts` (overlapping merges, conflict rate, resolution seconds, redo count)
  and the work waiting on it: cheap conflicts can justify more sharing, costly ones less. Put those
  numbers in `--why`. N counts every run on the file, the new one included. Exclusive files, budgets
  and `overlap_mode: fixed` still win. With `--overlap`, `--expires` ends only those file limits;
  without it an entry keeps its expiry (a new one has none). The plan's own expiry does not end them.
- A refusal that names `human` means a person wrote that item. Leave it as it is; if it blocks the
  work, ask the person (D71) instead of working around it.
- The goal is Jin's. Read it from `fa orch plan` at the start of every planning session and plan
  order, widths and assignments inside it; never redefine it. If it no longer fits, ask Jin (D71).
- An assignment wins over ordinary executor selection for that issue only. It never skips blockers,
  pauses, widths, hard limits or the cross review; when it lapses or is cleared, ordinary selection
  applies again. An executor the project does not know is refused.
- The plan's expiry does not lapse assignments or the goal.
- Every edit is recorded in `fa orch log` (`plan-pause`, `plan-resume`, `plan-queue`,
  `plan-width`, `plan-expires`, `plan-overlap`, `plan-expired`, `plan-assign`, `plan-assign-clear`,
  `plan-goal`, `plan-goal-clear`) with who, when and why.
