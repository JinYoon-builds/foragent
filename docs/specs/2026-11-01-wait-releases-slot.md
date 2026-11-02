# Waiting runs release the machine run slot

A live run that only waits for the merge turn or lock (merge-turn-wait, merge-lock-wait), CI results (ci), or file overlap (overlap-wait) releases its machine run slot. Ready work, including the highest-priority issue, can start on the next gate tick when these waiters would otherwise fill the machine budget. Implementing, checking, reviewing and resolving-conflict continue to occupy a slot. A waiter returning to base refresh and re-verification, conflict resolution or a new implementation attempt occupies a slot again before that work starts; when the shared budget is full it queues until a slot opens, with the same admission guarantees as a new start. Admission, public capacity counts, current facts and historical scorecard occupancy agree. Waiting runs remain visible and keep their run identity. The separate test-slot budget is unchanged.

Incident: 2026-11-01 07:15Z. Memory pressure had reduced the machine parallel budget to 22; 17 of 21 held slots were merge-lock-wait runs. Top-priority #1444 could not start for over ten minutes until a waiting run was stopped manually. The machine budget bounds CPU and memory demand, so process liveness alone is the wrong occupancy model.

Root cause at the checked base: capacity._holds_slot tests only launch grace/PID liveness; _store_scan counts every own active run when live=False, bypassing even that predicate. Filtering only _holds_slot or only sibling runs cannot fix normal gate admission. Start reservations and live workloads are part of the existing shared budget and must keep their current protections and deduplication.

The lifecycle gap is separate from counting: run_publish_flow leaves the run in ci while it follows a moved base, merges it and verifies again; merge-turn waiting and WaitPhase restoration also return directly to their caller. There is no machine re-admission when that idle run resumes work. Fixing just the phase filter would allow ready starts and returning waiters to oversubscribe the same budget.

Use one authoritative run-occupancy rule for admission, capacity functions, facts and scorecard replay. orch_facts_resources._machine_runs separately counts all own active runs and live siblings; orch_scorecard_records._occupancy separately charges whole execution lifetimes. Current facts must exclude open wait phases and historical replay must exclude only the recorded wait portions, retaining active portions and launch reservations. Keep facts observational and offline.

The four named phase identifiers are structured lifecycle facts, not classifications of natural-language meaning. Unknown/legacy phases and active work remain counted. A live workload still consumes its ordinary machine slot even if its parent run is waiting; an idle parent alone does not. Do not filter every workload just because its PID also belongs to a waiting run.

A returning waiter competes for the same machine budget as new starts, including across repository stores. It must wait before base-refresh Git work, verification subprocesses or a new model turn, count exactly once once admitted, and release the acquired occupancy on finish. It must not become an extra unbudgeted worker, disappear from facts, or require a human to stop/restart it. Existing launch, liveness, workload reuse, run-time/wait accounting and test-slot behavior remain intact.

Apply the lifecycle behavior wherever these waits return, including ordinary publication and the existing review-only publication path. Preserve existing review-only outcomes; this issue does not make an otherwise refused behind PR refresh or merge.

Public acceptance uses scheduler.tick with test_capacity_aimd.machine_repositories, real global priority, live offline run records and the existing launcher fake. Each case first proves that active work fills the budget, then changes only the holders to a named wait and expects [8, 7] to launch next without exceeding two occupied slots. Both own and sibling stores are exercised.

Public fa orch facts and capacity.machine_runs/machine_holders cover mixed active and waiting runs. fa orch scorecard is exercised with the existing flow fixture: an independent checking run keeps the only test slot occupied for an hour while another run works for forty minutes and waits for twenty; only the twenty waiting minutes create unused machine capacity. Tests call no private production occupancy or admission helpers.

runner.run_issue uses the existing make_case Git/tracker/executor fakes. Real CI, FIFO-turn or late-file waiting ends while another store fills a one-slot budget. External Git, check-process and executor boundaries reject any work before the slot opens; the fake clock supplies progress to release the blocker during the slot wait. Scenarios cover base refresh/re-verification, a real conflict and a failed-CI implementation retry. Production admission, phase and merge decisions are never replaced.

This one main/m issue owns every new case in tests/acceptance/test_wait_releases_slot.py and all behavior needed to pass it. The five existing locked files in Done when are unchanged regression controls, verified green on this base; none requires idle waits to occupy machine slots, so no replacement payload is needed. No blockers or other draft are required. Unit files listed in Files may be adapted for the lifecycle change while preserving their behavioral controls.

Expected implementation files (exact scope):
- `src/foragent/capacity.py`
- `src/foragent/run_waits.py`
- `src/foragent/run_merge_turn.py`
- `src/foragent/run_publish_ci.py`
- `src/foragent/run_publish_flow.py`
- `src/foragent/run_publish_reviewed.py`
- `src/foragent/orch_facts_resources.py`
- `src/foragent/orch_scorecard_records.py`
- `tests/test_capacity.py`
- `tests/test_capacity_workload.py`
- `tests/test_run_waits.py`
- `tests/test_run_merge_turn.py`
- `tests/acceptance/test_wait_releases_slot.py`

Lane: main, the implicit owner of capacity.py in .foragent.yml. Size: m. One issue, no blockers.

Out of scope: AIMD tuning or ceiling changes; project max_parallel semantics; new exemptions for other wait phases; test-slot admission, sizes or accounting; priority, FIFO, file claims or merge-group policy changes; CI evidence policy; new review-only base-refresh behavior; model judgment or new agent instructions; unrelated baseline failures and other orders.
