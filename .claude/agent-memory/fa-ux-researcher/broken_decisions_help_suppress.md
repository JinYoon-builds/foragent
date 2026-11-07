---
name: broken-decisions-help-suppress
description: fa decisions typo error still lists and suggests internal _sweep (help itself fixed by #2399)
metadata:
  type: project
---

Goal: a person learns the decision commands from the CLI.
Start: foragent repo, fa main after PR #2399 (order #2271), 2026-11-07.
`fa decisions --help` is fixed: only add/stop-check/drop.
Repro: `fa decisions sweep` (or any unknown subcommand).
Observed: exit 2, "invalid choice: 'sweep', maybe you meant '_sweep'? (choose from '_sweep', 'add', 'stop-check', 'drop')". Confirmed by a second session 2026-11-07; re-reproduced on main 816e722f and reported via fa orch request 2026-11-07 (saved "reported"; an earlier "reported" never showed in fa orch inbox, so grep the inbox for "invalid choice" before re-sending). Owner: decisions lane.
Re-check: remove this entry once the error names only add/stop-check/drop.
