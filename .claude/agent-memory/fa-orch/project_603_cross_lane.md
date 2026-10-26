---
name: project-603-cross-lane-start
description: #603 cross-lane start ranking in scheduler_start._ranked, global priority list in orch_queue; no main-last key
metadata:
  type: project
---
#603: `scheduler_start._start_ready()` ranks all (lane, number) together via `_ranked()`: (overlap count vs `overlap_files.claims()` taken once per tick, global list index, position in the queue-ordered lane list, issue number). Every start check still runs per candidate; lane holds are cached per lane.

**Why:** a first attempt added `lane_order(lane)[0]` (main last) before the number to keep two old unit tests green; review rejected it — the issue fixes the key list and the number tie-break wins over lanes. The old tests (`test_lane_single_run_cap_order_and_lowest_issue`, `test_project_ceiling_counts_only_this_repositorys_runs`) keep their names with updated expectations.

**How to apply:** do not add lane-based keys to start ranking. Global list lives in `<state>/global-queue.json` (`orch_queue.get_global/set_global`); CLI `fa orch queue --global N...` gets the first number in the optional `lane` positional. CLI text is in `messages/orch_queue.py`.
