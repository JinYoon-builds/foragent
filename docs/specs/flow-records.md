# Local flow records and approval-to-merge scorecard

## Confirmed problem and scope

At base a7d901093896225e7849f9240ef0887e6e528ce2 (latest locally available origin/main; the original HEAD is left unchanged), existing decision branches lose their cause and mutable observations overwrite their history. scheduler_start.py:226 breaks on slot exhaustion; overlap_files.py:196 returns only a boolean; run_merge_turn.py:124 drops withdrawal cause; ci_mode.py:353 retries rejected evidence without retaining that history. state_observations.py:258 and state_refresh.py:69 overwrite labels and dependencies, while orch_tick_outcome.py:137 and orch_queue.py:56 overwrite timing and priority. The existing idle counter ORs run/test availability, and orch_scorecard.py:65 calls finish-minus-start minutes-to-merge. These are observation and accounting defects. Existing branch decisions are not to be redesigned.

This order adds local evidence and the person's five-part scorecard. It changes no merge, start or check behavior. No new semantic judgment rule, keyword list, similarity threshold, model decision in a script or agent step order is authorized. Only recording existing factual outcomes and deterministic accounting is in scope. The supplied prior scan was independently checked against this base; the complete source/privacy/turn audit is in the accompanying local DIAGNOSIS.md. The required outcome and interfaces are fully defined here, so publication does not depend on that external scan or the user's survey script.

The named survey flow5.py was read. Its six labels and definitions are included below. Its private imports and approximate retrospective guesses are not an implementation prescription.

Remote freshness could not be verified: git ls-remote failed with DNS resolution of github.com. No fetch, ref mutation or switch was performed. During drafting another session advanced local origin/main. The four-file delta was inspected, including the changed manual merge reservation and withdrawal behavior. Source and tests for this exact tip were read with git archive inside the allowed draft directory, and focused validation ran there. The original worktree HEAD remains ddbaf1b984be423cc6a30fdf50e636ad27c81d32. Refresh/revalidate before later publication if origin/main advances again.

## Contracts

The first issue supplies two public StateStore methods used by the following issues and their locked tests:

- `record_flow(kind, *, issue=None, run_started_at=None, **data)` records an observation at the store's UTC clock.
- `flow_records(*, since=None, until=None)` returns chronological local observations in `[since, until)`, with omitted bounds open. Each observation exposes `at`, `kind`, `issue`, `run_started_at` and `data`. These are the cross-issue interface, not a storage format. Read calls have no side effects.

The semantic kinds are `issue-state`, `ready`, `priority`, `tick`, `overlap`, `merge-withdrawal`, `ci-evidence`, `spawn`, `stop`, and the minimum `activity` provenance needed for the split. `issue-state` carries sorted labels, native blocked_by facts and open/closed state; `priority` carries the normalized ordered `issues` list. Tick data exposes `status`, `duration_seconds` and any measured step timings. Each acquired completed or failed tick is an occurrence, even if the measured duration repeats. A tick that failed to acquire the lock records nothing.

A `ready` transition has the existing branch's `reason`, and `outcome` (`waiting` or `started`). Known slot decisions carry `run_slots = {limit, occupied, merge_waiters}` using the same effective scope as the decisive limit, including a project ceiling where it actually bound. Required reason codes include `slots-full`, `free-slot`, `held-no-retry`, `blocked-by`, and `file-overlap`; other existing outcomes remain distinguishable. Starting clears the reason to null. `free-slot` is a measured eligible gap, never a guess from spare test capacity. A blocked_by history records each known edge's state and repository, not merely today's blocker count.

An `overlap` transition carries `holds`, each with the limiting `file`, effective `limit`, and `peers` identified by issue and run_started_at. Empty holds records release. Missing-file lane serialization and exclusive reservations retain their actual distinct cause, without fabricating a numeric per-file limit. `merge-withdrawal` exposes `reason` and `detail` for each real withdrawal; `merge-refused` names a refused merge. Public legacy callers that provide no cause remain explicitly unknown. A `ci-evidence` transition exposes `refused`, actual `reason`/`detail`, and requested `base_sha`/`head_sha`; acceptance closes the refusal with refused=false. Identical evidence polls do not add transitions. `spawn` and `stop` retain actual skipped or completed decisions; tests name `disabled` and `not-running` for those existing branches.

Known activity provenance exposes `activity`, `cause`, `started_at` and `finished_at`, with execution identity. Causes include `initial`, `base-changed`, `group-failed`, `file-overlap`, and `queue`. Retain facts already known where work is invoked; do not classify human text or assume that any check after joining a queue is concurrency cost. Reuse existing phase/work/merge records wherever they already establish the same result. Concurrent or replayed observations must not multiply one transition or interval. Storage filenames, private helpers, rotation, serialization and indexes belong to the implementing lane.

Locality is part of the observable contract: records stay in the user's local StateStore data, outside published repository artifacts, GitHub comments, check artifacts, telemetry, push payloads and upstream reports. Existing business notifications and comments keep their present behavior. Recording cannot introduce network calls, extra check/merge/start attempts, new ordering, sleep, deadline changes or new agent instructions. An observational I/O failure must not replace the original return value or exception or prevent ordinary work.

All generations of new flow storage together are bounded: at most 2,048 retained records and 8 MiB per repository, including record indexes/continuation state. The newest observation remains readable; closing an issue is not a pruning trigger. A retained boundary must not rewrite earlier facts. Missing, malformed, pre-installation or evicted evidence is unknown, never a zero or inferred historical cause. Reading a recent scorecard scans bounded retained evidence, not growing lifetime archives, runs no subprocess or network call, and does not rewrite records. The fixture sends 2,200 distinct 2 KiB observations to exercise the finite bound. This upper bound is an explicit draft assumption; it does not promise a fixed number of covered days under unlimited tick volume.

## Approval-to-merge accounting

The scorecard's JSON windows expose `approval_to_merge = {total_minutes, parts}`. `parts` has exactly `work`, `concurrency_cost`, `machine_limit`, `ordering`, `waste`, and `unknown`, each a nonnegative number of issue-minutes. The six values sum to total_minutes without rounding whole minutes. Never force unknown into one of the five parts.

Use each confirmed merge once and its latest valid approval at or before that merge. Assign an issue's entire approval-to-merge interval to the window containing its actual merge. Include first-start and between-run gaps as well as time inside runs. Runs/retries within that interval do not restart the lead clock. A confirmed merge followed by cleanup retains its actual merge time. No confirmed merge or no known approval is missing coverage, not zero-minute success. A `merge-queue` turn result alone never confirms a merge.

Today's split totals the cohort merged in the last 24 hours. The previous split totals the cohort merged during the earlier days selected by --days and is labeled previous-days total, not a median. These additive cohort totals are separate from unchanged metrics' existing daily-median baseline. Intervals from different issues contribute their own issue-minutes; multiple explanations or duplicated observations for the same issue/time contribute once. Corroborating evidence agrees; contradictory evidence without a decisive factual cause is unknown. Do not resolve ambiguity by an arbitrary category precedence.

1. Work: implementing, reviewing, initial checks/baselines and first CI for the change, plus the actual merge operation. Ordinary work is not waste merely because an attempt later failed. A later review/implementation whose cause is unclear stays unknown rather than being forced into concurrency.
2. Concurrency cost: resolving a conflict, verification and CI repeated because main was integrated, individual verification caused by a failed group, and file-overlap waiting. Use explicit operation/base/group provenance rather than the first-queue heuristic in the survey script. A neutral base move that reuses evidence adds no recheck cost.
3. Machine limit: a measured relevant slot limit really full of productive holders. Spare test slots do not imply spare run slots. A budget/holder count that is missing, a usage-plan pause, a GitHub wait or a failed quota reread does not prove machine fullness.
4. Ordering: a recorded live blocked_by dependency, for the period it actually blocked. A dependency later closed or removed does not erase the earlier interval. A generic plan pause alone is unknown under this five-part definition, rather than mislabeled quota/fullness.
5. Real waste: standing in the merge queue, eligible work not started with a measured free run slot, ready work denied productive capacity because merge waiters hold slots, held work with no retry underway, and measured scheduler/tick delay. Queue standing, retry spacing and actual merge-lock acquisition must stay distinguishable where their existing branches know the difference. Unexplained flock contention is unknown, rather than automatically queue waste. Tick duration is not automatically waste; only a delay in this issue's progress established by the records contributes. Overlap waiting is concurrency, not waste.

Use the seven requested record families to reduce unknown when the observed facts establish one of these definitions. Historical evidence does not become known just because the current state looks similar. Preserve the survey's definitions, not its 1-minute sampling, hardcoded incident exclusion, private imports, current-label guesses or late-queue heuristic.

Expose `run_slot_idle_minutes` and `test_slot_idle_minutes` separately as wall minutes with eligible demand and a measured unused slot of that kind. They are separate resource measurements and may overlap. Keep any legacy OR-based idle value only as explicitly named legacy combined idle, not the new run-idle meaning. Wait-by-cause says it covers time inside runs only. `median_minutes_to_merge` and `lead_time_minutes` measure latest approval to actual merge; absent approval remains unknown. Human output names approval, both slot types, all five parts, unknown and the relevant window/units. Existing waste_share/facts runtime measurements, if retained, identify their actual denominators and do not masquerade as the new real-waste part.

## Turn result and outbound boundary

run_publish_flow.py:243 records merged before merge_pr at :245; :247 appends failed on TrackerError. A hard crash can leave a false merged row; ordinary failure leaves both rows. run_publish_reviewed.py:211 and orch.py:568 do the same. run_merge_turn's production reader derives eligibility from ready/withdrawn and live runs, not turn.result. Current scorecard readers use run/record_merge evidence, written in the API success branch at run_publish_flow.py:256. The misleading turn history deserves a separate repair, but changing its schema or API-call ordering is outside this order: none of these measurements may treat that row as proof of success. The locked scorecard control seeds such a false row.

The source audit found no direct exporter of a new flow record stream. Existing CI flags can send diagnostic text to GitHub; notifications can forward blocked/outage/error summaries via configured channels; approved upstream error reports send allowlisted package/error/numeric data. CI uploads generated acceptance shard reports. New flow records must not enter these payloads, generic notification summaries or committed worktree files. Local scorecard/brief rendering is permitted. This boundary does not change existing notifications, reports, checks, or source publication.

## Delivery and verification

Three independently verifiable medium issues replace one oversized feature. State/record infrastructure is first; runner/CI decisions depend on it; the scorecard depends on both. Existing lane orch owns orchestration/storage collection and scorecard; implicit main owns runner/CI/overlap paths with no configured dedicated lane. No new lane or agent role is created. Files are listed individually in each child. The lane chooses implementation and private structure.

The acceptance files embedded in this order are locked. Public scheduler ticks, runner calls, lifecycle entries, queue updates, StateStore observations and scorecard CLI calls run with offline trackers, fake executors/process tables and controlled clocks. Tests never replace a policy/selection/calculation helper. Synthetic scorecard history uses the declared public record contract. Storage interface tests do not choose a storage layout.

Against the pinned latest origin/main, 76 focused original cases passed before installing the new locks: 72 locked acceptance cases and four existing manual-premerge withdrawal controls. The proposed locks then produced 12 expected new failures and five passing existing scorecard cases in 10.65 seconds. Final validation details are in DIAGNOSIS.md. No post-implementation result is claimed. Each new test must fail here for missing records or changed measurement semantics and pass after implementation. No full suite is a Done when command. The existing scorecard helper gains explicit approval facts for scenarios whose approval and start coincide; all old assertions remain intact. A legacy combined-idle measurement may remain alongside the two correctly named counters. Original locked files remain untouched in this working tree. The plan embeds one explicit scorecard fixture migration for later publication; its assertions are unchanged. The new acceptance governs the explicitly requested replacement meanings. Legacy OR-idle remains only as clearly named legacy combined idle for compatibility.

The golden completed issue has 63 approval-to-merge minutes: 21 work, 17 concurrency, 5 machine, 4 ordering, 14 real waste and 2 unknown. Other checks cover fractional minutes, missing evidence, one occupied run slot with spare test capacity, 90-minute approval lead versus 10-minute run/20-minute cleanup, and a false merged turn row. The complete order does not run or publish itself.

### flow-record-state

Goal: Ready issues expose why they did not start on each tick, with one record only when that issue's effective reason or decisive facts change, including release/start. All candidates held by an already decisive global branch get that known cause, even when the original loop breaks; do not execute later skipped checks or make extra network reads to manufacture a cause. Capture accepted label, blocked_by and open/closed transitions, including initial observations, local writes, refreshes and edge-only changes. History survives close, complete absence and StateStore reload within retention, and stale observations do not enter history. Retain each acquired tick's duration/outcome and every changed normalized global priority list, including clear. Supply the public record contract and finite local storage described under Contracts. Preserve normal starts, errors, reports and all existing decision results.

Out of scope: Changing merge/start/check behavior, priority or admission policy, retry cadence, quotas, limits, process safety, notifications or approval boundaries; adding semantic judgment rules or agent step orders; repairing pre-API merged turn results; recording unrelated full configuration/transport/session history; publishing this draft or weakening locked assertions; changing existing locked files outside the explicitly embedded scorecard fixture migration.

Files:
- src/foragent/state.py
- src/foragent/state_observations.py
- src/foragent/state_issues.py
- src/foragent/state_refresh.py
- src/foragent/state_logs.py
- src/foragent/scheduler.py
- src/foragent/scheduler_start.py
- src/foragent/orch_tick_outcome.py
- src/foragent/orch_queue.py
- tests/test_state_observations.py
- tests/test_orch_tick_outcome.py
- tests/acceptance/test_flow_record_state.py

Done when:
```sh
uv run pytest -q tests/acceptance/test_flow_record_state.py
```
```sh
uv run pytest -q tests/acceptance/test_observation_ingest.py
```

### flow-record-decisions

Goal: At existing decision points, retain the exact overlap blocker executions, limiting file/pattern and effective limit; actual merge-queue withdrawal reason; each changed CI-evidence refusal and subsequent acceptance with requested revisions; and why orchestrator spawn or issue stop was skipped. Record releases, genuine withdrawals and repeated episodes without duplicating polls, calls on an already absent seat or already known stop-refusal evidence. Capture admission and late-file holds; exclusive and no-Files cases keep their actual cause. Preserve all known skip branches, including spawn disabled/no-work/live-session/spacing/unavailable launch and stop absent/unsafe/refused targets. Keep enough existing factual base-refresh/group-failure and activity timing provenance to distinguish initial work from concurrency verification, queue standing from retry spacing/lock contention, and productive occupancy from merge-waiter occupancy. The public record interface and payload meanings are in Contracts. The scheduler's existing no-start, free-file control, merge retry and CI repair outcomes remain identical.

Out of scope: Changing merge/start/check behavior, priority or admission policy, retry cadence, quotas, limits, process safety, notifications or approval boundaries; adding semantic judgment rules or agent step orders; repairing pre-API merged turn results; recording unrelated full configuration/transport/session history; publishing this draft or weakening locked assertions; changing existing locked files outside the explicitly embedded scorecard fixture migration.

Files:
- src/foragent/overlap_files.py
- src/foragent/run_overlap.py
- src/foragent/run_merge_turn.py
- src/foragent/run_publish_flow.py
- src/foragent/run_publish_reviewed.py
- src/foragent/run_publish_merge.py
- src/foragent/ci_mode.py
- src/foragent/orch_spawn.py
- src/foragent/orch_ensure.py
- src/foragent/orch_stop.py
- src/foragent/scheduler_stop.py
- src/foragent/run_group_check.py
- src/foragent/run_group_ci.py
- src/foragent/run_waits.py
- src/foragent/state_phases.py
- src/foragent/orch.py
- src/foragent/orch_spawn_launch.py
- src/foragent/run_checks.py
- src/foragent/run_publish_ci.py
- tests/test_run_merge_turn.py
- tests/test_ci_mode_unit.py
- tests/acceptance/test_flow_record_decisions.py
- tests/test_orch_premerge.py

Done when:
```sh
uv run pytest -q tests/acceptance/test_flow_record_decisions.py
```
```sh
uv run pytest -q tests/acceptance/test_merge_turn_priority.py tests/acceptance/test_merge_wait_budget.py tests/acceptance/test_scorecard_records.py tests/test_orch_premerge.py::test_orch_merge_withdraws_reservation_when_premerge_cannot_finish
```

### flow-scorecard-split

Goal: Expose the exact approval-to-merge accounting defined below in JSON and human scorecard output, using the two record issues' shared public contract. The five parts plus unknown are nonnegative and sum to their cohort total; intervals and retries are counted once, including before first start and between runs. Historical label/dependency/slot/priority evidence remains usable after issue close/absence, while missing or evicted evidence stays unknown. Report run-slot and test-slot idle separately, describe wait-by-cause as inside runs only, and make minutes-to-merge consistently start at latest approval and end at actual confirmed merge. Distinguish concurrency rechecks from initial work using factual provenance, not a heuristic based on when a queue was first entered. Read only local bounded evidence with no mutation, network or subprocess, and ignore unconfirmed merged turn rows. Human output names all parts, units, windows and unknown coverage. Preserve unrelated scorecard measurements under their existing contracts and identify legacy waste/runtime denominators explicitly.

Out of scope: Changing merge/start/check behavior, priority or admission policy, retry cadence, quotas, limits, process safety, notifications or approval boundaries; adding semantic judgment rules or agent step orders; repairing pre-API merged turn results; recording unrelated full configuration/transport/session history; publishing this draft or weakening locked assertions; changing existing locked files outside the explicitly embedded scorecard fixture migration.

Files:
- src/foragent/orch_scorecard.py
- src/foragent/orch_scorecard_flow.py
- src/foragent/orch_scorecard_records.py
- src/foragent/messages/orch_scorecard.py
- src/foragent/messages/presence.py
- tests/test_orch_scorecard_unit.py
- tests/acceptance/test_flow_scorecard_split.py
- tests/acceptance/test_orch_scorecard.py

Done when:
```sh
uv run pytest -q tests/acceptance/test_flow_scorecard_split.py
```
```sh
uv run pytest -q tests/acceptance/test_scorecard_flow.py::test_lead_time_uses_latest_approval_and_actual_merge_time tests/acceptance/test_orch_scorecard.py
```

## Existing locked fixture migration

The existing locked scorecard seed helper is republished with explicit approval-at-run-start only when the caller supplied no approval. These legacy scenarios intend those moments to coincide. This supplies a missing input without changing an assertion or adding a production fallback; explicit earlier approvals remain authoritative. The new missing-history test separately requires unknown when approval/causal evidence is absent. This is a declared locked-fixture migration in acceptance_files, not permission for the lane to weaken locked assertions. The original and proposed files each pass all five existing cases on this unchanged source.
