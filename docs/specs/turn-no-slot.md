# Remove machine re-admission deadlocks for merge-turn owners

## Problem and classification
The reported post-#1447 incident left #999 holding the merge turn for over 30 minutes with green CI, #998 behind it, and no merge for 44 minutes. These incident facts are supplied by the requester. The defect is resource inversion: a run owns the merge turn but waits for machine-run capacity that progress through that turn helps release. The first draft's merge-only exception missed the same inversion during necessary local work. That omission was a planning judgment error; the correction is the complete path inventory and executable coverage, not semantic rules or agent step orders in a script. The admissions and their unbounded wait are tool defects.

## Required behavior and per-path choices
For every target, a live run that owns that target's merge turn proceeds without waiting for machine-run budget, even when four other live workers occupy a budget of two. Ownership alone grants this exception, not green CI, a label, issue identity, PID liveness alone, or being in a wait/merge phase. Keep the turn, existing priority, approval-backed claims, completed implementation/review and final merge safety checks. Count real local work and release its capacity through normal completion.

| Path while holding the turn | Chosen result |
| --- | --- |
| Final merge, including a return from merge-lock contention | Proceed without machine-run admission waiting; keep final merge serialization and exact-head/evidence checks. |
| Implementation base refresh, whether main moved before finalization or during the final snapshot | Proceed with local refresh, baseline capture and re-verification; retain the turn. |
| Review-only base refresh and re-verification | Proceed under the same rule; retain the turn and reviewed head provenance. A base conflict that cannot carry the review still stops through the existing recoverable path, without a machine-budget wait. |
| Conflict resolution, further implementation needed to resolve it, and its checks | Proceed without machine-run admission waiting; retain prior review where the current conflict flow already does so. |
| Individual/local-only/protected re-checks and nested phase restoration | Proceed without machine-run admission waiting. No verification may be skipped or relabeled to obtain the exception. |
| Joint local/CI group verification, CI-to-checking restoration and temporary group-resource cleanup | Proceed without machine-run admission waiting; preserve measured evidence and cleanup. |
| Return from late overlap or another nested idle phase while still owning the turn | Proceed without machine-run admission waiting; keep the separate resource's existing exclusion rules. |

Reviews occur before joining the merge queue. A CI failure detected after obtaining the turn must also pass its transition into retry without a machine wait; the existing exception unwind then withdraws the queue entry before the next implementation/review attempt. An already admitted slot remains held until its normal release. A retry that never owned the turn keeps normal admission; no new reviewer exception is needed.

Nonholders still wait for their turn and for normal machine-run admission. Every blocking run re-admission attempt has a limit: at most 600 seconds, capped by the run's remaining work allowance measured at entry. If no capacity opens, finish this attempt recoverably, keep the approved open PR head/approval, make it eligible for retry, and release waits/claims/capacity. No human block or false merge; no endless admission loop. Initial starts retain their existing admission and timeout rules. While a nonholder waits for machine capacity, its recorded phase identifies a slot wait rather than CI. Credited queue time and normal active-work runtime limits remain intact.

## Scope and ownership
One unattended issue in the existing orch lane, which owns the public runner lifecycle in .foragent.yml. Implementation structure, helper names, phase spelling and APIs belong to the lane under D98. This order defines resource behavior, not agent judgment or an agent workflow. Independently held test-suite slots retain their existing limits; this issue addresses the machine-run budget. It does not increase AIMD, machine/test-slot limits, change priority, bypass acceptance/CI, or alter unrelated expected-red cases.

## Locked results and conflicts
The new public-runner tests cover implementation and review-only merge, return from a predecessor's turn, a full-budget nonholder, implementation and review-only refresh, real conflict resolution/re-checks, truthful waiting, normal admission after a slot opens, bounded recovery when no slot opens, exactly one checked-head merge, and capacity cleanup.

The existing wait-releases-slot lock conflicts in exactly three parameter instances: ci-refresh, turn-refresh and ci-conflict. They currently require a turn holder to wait for a machine slot. Its complete owner-corrected replacement retains every node id and all unrelated assertions; those cases now require counted progress while retaining the turn. overlap-refresh and ci-retry still require normal admission.

The complete replacement of tests/acceptance/test_merge_path_real_settings.py changes no original test body, decorator or node id. It adds one parametrized case with individual-refresh and group-check-and-return variants. Real settings, scheduler, public runners, Git, measured pytest CI, comparisons, queue, phases and cleanup execute; only external sensors/occupancy and existing external model/GitHub transport fakes are supplied. Four real runners must merge their own verified heads while four other workers stay live at budget two; moved main must survive and group resources must close. A watchdog only releases test-owned external occupancy so broken source cannot hang the test; needing that release fails the case. No existing expected-red entry is changed.

All three full acceptance contents are embedded in acceptance_files and match the staged copies byte for byte. Run only the named target selections, serially, under nice -n 15 and Perl alarms. Collect on an exact HEAD archive with only this plan's acceptance_files overlaid; no prototype or caller-only helper may satisfy collection.

## Files
- src/foragent/runner.py
- src/foragent/capacity.py
- src/foragent/run_merge_turn.py
- src/foragent/run_waits.py
- tests/acceptance/test_turn_no_slot.py
- tests/acceptance/test_wait_releases_slot.py
- tests/acceptance/test_merge_path_real_settings.py
- docs/specs/turn-no-slot.md
