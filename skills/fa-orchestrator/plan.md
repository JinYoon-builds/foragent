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

- A pause needs both `--until` and `--why`; it lapses by itself when `--until` passes.
- Times are ISO; a time without a timezone is local time. Past times are refused.
- When the plan expires, everything in it lapses and lanes follow the defaults. The next edit starts a
  fresh plan with no expiry.
- Without a width a lane has no fixed cap: free slots fill with ready issues whose files do not
  overlap running ones. Use a width only to hold a lane back; use a pause to stop it.
- A refusal that names `human` means a person wrote that item. Leave it as it is; if it blocks the
  work, ask the person (D71) instead of working around it.
- Every edit is recorded in `fa orch log` (`plan-pause`, `plan-resume`, `plan-queue`,
  `plan-width`, `plan-expires`, `plan-expired`).
