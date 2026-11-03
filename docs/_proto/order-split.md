# Rebuild on main through ordered issues

Historical prototype document; see the [current-main port report](merge-owner-port-report.md)
for the port's compatibility decisions and verification.

Keep this prototype uninstalled. All six reported failures are tool defects:
availability, resource/generation identity, durable transitions or missing input.
They require no new agent judgment rules. Estimates below are engineering days
including focused tests/review, assuming one person familiar with this code.

Merge the integration harness FIRST, then measurement, CI-red correctness, and
orders batching. Each numbered issue depends on its predecessor; each must leave
main independently verifiable. Use explicit strict expected-red entries for
known unmet behavior; remove each entry only in its owning implementation issue.

1. **I1 — executable integration contract (M, 1–2 days).** Port
   `test_merge_owner_e2e_sandbox.py` to main's existing public runner/tick APIs;
   land the isolated HOME, bare origin and executable JSON gh service first.
   Pin four-issue commit order, worker exit, lost-test repair and restart replay.
   Main's legacy worker waits need bounded fixture release, not hanging workers.
   Carry the sandbox transport; adapt its owner launcher when I5 lands.
2. **I2 — measure before optimizing (M, 1–2 days).** Record gate-wide gh calls,
   CI rounds, test CPU/slot use, queue dwell and active owner time for 4/40 items.
   Carry Snapshot and immutable CI reports. Prove repeatable counts and unchanged
   locked-read freshness with deterministic bounds; report real timings separately.
   The prototype's 280-call observation covers pending owner work, not whole ticks.
3. **I3 — CI red and missing evidence (M, 1–2 days).** Keep ordinary CI failure,
   lost passing tests, rejected producers and transport unavailability distinct.
   Carry shared ci_mode/ci_local comparison and command discovery. Prove red/lost
   tests repair, missing/stale evidence waits, rejected artifacts hold/fallback,
   and a throttled read or merge preserves every admission with a shared pause.
4. **I4 — orders batching on main (M, 1 day).** Reuse order_waiting, order_flush
   and their journal; let the agent supply urgency and the named boundary.
   Verify two deferred approved orders publish once at that boundary, urgent
   publication leaves them waiting, and restart does not duplicate publication.
   Carry existing D85 tests; this owner prototype supplies no new batching proof.
5. **I5 — durable worker/owner handoff, N=1 (L, 2–3 days).** Carry priority seats,
   enqueue's verified publication payload, expected-head guards and final locked
   reads. Rewrite lifecycle into explicit worker generation/admission/progress
   records; use the established independent-process lock transfer. Prove worker
   exit frees capacity, scheduler remains available during checks, and restart
   owns only recorded child birth identities. Keep owner mode opt-in initially.
6. **I6 — replay and completion (M, 1–2 days).** Carry close_for_repair and merge
   identity validation. Rewrite transitions so intent precedes I/O and labels;
   bind merge facts to run and approval generations, with parsed timestamps.
   Verify lost-close/merge responses replay, newer approvals remain open, and
   scorecard/ownership count each merged execution once. Keep preserved branches.
7. **I7 — availability and work budgets (M, 0.5–1 day).** Carry orch_github's
   shared pause and the per-advancement budget fix. Make owner bootstrap and all
   retry boundaries honor the same wait. Prove one exhausted candidate cannot
   time out an untouched one; paused passes issue no admission requests.
8. **I8 — migrate and retire legacy publication (M, 1–2 days).** Drain workers,
   enable the owner, verify both settings modes, then remove worker waiting and
   duplicate direct-merge tails. Carry shared verification/tests, not two engines.
   Verify enable/disable/restart and batch-parent targets before defaulting on.
9. **I9 — combined CI/group reuse (L, 2–4 days; separate release).** Carry group
   evidence/provenance/bisection, rewrite ownership for owner-side groups. Prove
   one measured combined head covers ordered member merges, and failed/void or
   outsider changes fall back correctly. N>1 is not established by this sandbox.

Locked contracts: **0 edited in this repair; 8 need approved revisions during
I5–I9 retirement**, retaining their safety assertions while replacing live-worker
waits, worker `merged` outcomes and worker-owned group fixtures:
- `tests/acceptance/test_merge_turn.py`
- `tests/acceptance/test_merge_turn_priority.py`
- `tests/acceptance/test_merge_wait_budget.py`
- `tests/acceptance/test_merge_turn_before_premerge.py`
- `tests/acceptance/test_merge_group.py`
- `tests/acceptance/test_merge_group_bisect.py`
- `tests/acceptance/test_ci_group_evidence.py`
- `tests/acceptance/test_merge_path_real_settings.py`

Budget: I1–I8 roughly 8.5–15 days; I9 adds 2–4. Reuse roughly two thirds of
verification/identity/snapshot code, but rewrite lifecycle/accounting boundaries.
Unproven: live GitHub latency/races, production launcher/signals, running-worker
migration, independent state directories, hook exactly-once behavior, group reuse,
batch-parent publication, orders integration and full legacy compatibility.
The sandbox uses real commits via fast-import/fetch, not forbidden commit/push;
models, sensors and owner launch transport are fake, and the HOME is asserted.
Verification (default temp, sequential): sandbox **1 passed in 49.12s**;
regressions **11 passed in 55.36s**; prototype **66 passed in 394.97s**.
Locked merge-path: **5 passed, 3 xfailed in 133.94s**. [Commands and limits](merge-owner.md#verification-for-this-revision).
