# Keep preread valid across lane-memory changes

## Problem
On 2026-11-01 and 2026-11-02, about five preread refusals were reported after lane learning, including a flush of four orders. The issue and its locked checks had not changed. The current preread key includes the execution prompt, which contains lane memory. Every lane merge can therefore retire otherwise clean evidence. No decision was found that intentionally binds preread to memory.

## Goal
A merge that changes only a lane's memory preserves its recorded clean preread. Both immediate approved publication and a deferred approved order's later flush accept that evidence without another model call. A four-order batch still publishes each approved child exactly once.

Keep invalidation when the issue content, locked tests, executor, model or lane instructions change. The public CLI keeps its current diagnostic: "no preread of the current content for issue(s) ...; run fa order preread PLAN.json first". A deferred order retains its submitted content; changing the caller's draft does not change that retained order. Flush still rechecks the current executor, model and lane instructions.

Creating an unrelated lane must also leave a waiting order's preread usable, as the existing publication lock requires. Run that existing lock unchanged alongside the new checks.

The implementing run receives the latest matching earlier APPROACH/DECIDE plan under the existing approved-body and locked-file matching rule. It also receives current lane memory, including pending lessons. Neither preread nor implementation loses memory context. Cover Codex and Claude, newly created memory, consumed pending lessons and Claude's partial native-memory notice.

Keep the comment at preread_target truthful: preread and the first implementation attempt can receive different current memory while using the same work and execution context. Leave the implementation design to the order lane.

## Out of scope
Question parsing, the reviewed statement-test route, approval selection, publication boundaries, proposal matching rules and unrelated failing locks stay outside this order.

## Files
- src/foragent/order.py
- tests/acceptance/test_preread_not_memory.py (locked by this order)

## Lane and skills
Use the existing order lane. Keep its fixed skills; the lane selects any per-issue tools and review skills.

## Done when
Run the supplied locked acceptance file and the existing unrelated-lane publication lock with the normal repository tooling, one invocation at a time, with a perl alarm:

```sh
perl -e 'alarm 180; exec @ARGV' uv run pytest -q tests/acceptance/test_preread_not_memory.py
perl -e 'alarm 180; exec @ARGV' uv run pytest -q tests/acceptance/test_d85_flush_recovery.py::test_orders_adding_lanes_publish_immediately_and_never_join_batch_recovery
```

The checks drive the public preread, publish, flush and gate run-issue CLI with offline Git, tracker and executor adapters. They check publication results and delivered context, without naming a new internal API or prescribing an implementation.
