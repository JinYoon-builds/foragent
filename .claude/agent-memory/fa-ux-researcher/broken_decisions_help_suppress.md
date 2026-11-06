---
name: broken-decisions-help-suppress
description: fa decisions --help leaks internal _sweep subcommand with raw ==SUPPRESS== text
metadata:
  type: project
---

Goal: a person learns the decision commands from `fa decisions --help`.
Start: foragent repo, fa 0.1.0 (a8b2765d), 2026-11-06.
Repro: `fa decisions --help`.
Observed: list shows `{_sweep,add,stop-check,drop}` and `_sweep  ==SUPPRESS==`. Confirmed by a second session; reported via fa orch request 2026-11-06.
Re-check: remove this entry once help shows only add/stop-check/drop.
