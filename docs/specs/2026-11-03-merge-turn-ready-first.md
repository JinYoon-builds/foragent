# A ready run merges while the merge-turn holder refreshes

## Measured defect (2026-11-03)

Source: `merge-queue.jsonl` and `orch-events.jsonl` in the project state folder.
The machine had merge group limit 1 and mode fixed.

- 02:32: #1477 joins the merge queue.
- 02:46: #1490 joins. 02:56: #1489 joins. Its PR is current (mergeStateStatus CLEAN) and every check is green.
- 02:52: #1477 gets the merge turn. Main had moved, so it refreshes onto the new main. It runs local checks,
  waits for test slots and runs CI twice. It keeps the turn for 37 minutes.
- 03:30: #1477's CI fails and it withdraws. #1490 and #1489 wait in merge-lock-wait the whole time.

## Cause

The merge turn goes to the first live ready entry in the queue (`src/foragent/run_merge_turn.py`,
`holds_turn`, line 144). A run that holds the turn and finds main moved refreshes and goes back to
CI without leaving the queue (`src/foragent/run_publish_flow.py`: `await_turn` at line 290, then the
refresh at line 304 and the in-lock move refresh at line 409, both through `refresh_base` at line 80). While it
refreshes and waits for CI, it is still the first live ready entry, so it holds the turn. No run behind
it can merge, even one that is current and green. The reviewed path (`src/foragent/run_publish_reviewed.py`)
uses the same turn (`await_turn` at line 177, `refresh` at lines 120 and 186).

## Wanted result

A run that holds the merge turn but cannot merge now (it must refresh, re-check or wait for CI) does not
block a ready run whose PR is current and green. The ready run merges first. The holder keeps a fair
place: it is not sent to the back of the queue and it is not starved.

How is the lane's choice. Two options: the holder yields the turn for the whole refresh, or the turn
skips holders that are not ready now. The queue rules stay: each run appends only its own entries, the
file only grows, and the turn is derived on read, never stored.

## A constraint from an existing lock

`tests/acceptance/test_turn_no_slot.py::test_turn_holder_local_work_proceeds_without_capacity_wait`
checks that a lone holder still holds the turn, and skips the machine-capacity wait, while it refreshes
and re-runs checks. So a holder may give the turn away only to a run that is ready now. With no such run,
it keeps the turn and its capacity exemption.

## Prototype (proves the lock, not the design)

`prototype.patch` adds an own-seat `aside` event. `refresh_base` appends it. The holder's next `ready`
(when it rejoins after CI) clears it. `holds_turn` gives the turn to the first live entry that is not
aside, and falls back to the first live entry when every live entry is aside. The holder keeps its
position. The prototype covers only the flow path. The lane must also cover the reviewed path
(`run_publish_reviewed.py`), the human `claim` path (`run_merge_turn.claim`), and any reader of the queue
in status or flow records that names the turn holder.

## Done when

- `uv run pytest -q tests/acceptance/test_merge_turn_ready_first.py`: the holder refreshes and waits for
  its second CI. The second run, verified on the moved main and green, merges while that CI is held.
  Then the holder merges, and its head contains the second run's merge.
- The existing merge-turn and merge-queue locks keep passing: `test_merge_turn.py`,
  `test_merge_turn_priority.py`, `test_merge_turn_before_premerge.py`, `test_turn_no_slot.py`,
  `test_merge_wait_limit.py`, `test_merge_wait_budget.py`.
- `test_merge_group.py` is not a Done when line: 13 of its 18 tests fail on clean main
  (afd1b174) already. The prototype fails the same 13 and passes the same 5.
