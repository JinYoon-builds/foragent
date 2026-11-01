# Human premerge excludes unmerged approved locks

## Goal
Human `fa orch premerge` and `fa orch merge` must not report a locked acceptance test owned by another open, approved, unmerged issue as broken. This includes a PR merge result containing tests published on main after its target branch diverged. A protected test that passed on the target base and fails on the merge result must still prevent the human merge.

## Evidence and diagnosis
The PM reported two refusals of PR #1298 on 2026-11-01, with only the locked tests for approved, unimplemented orders #1336 and #1324 in the second broken list. Offline source inspection and public-entry reproduction identify a coverage gap: `orch.py:359` selects the fetched PR target SHA; `orch.py:401-402` combines it with the PR head. `orch.py:415` computes exclusions against that target SHA, while `run_baseline.py:183-186` drops approved-owned paths absent there. A head containing newer main commits can bring those paths into the merge result anyway. CI-style premerge applies the filtered ignores (`orch.py:420-424`); the local path does not apply ignores to the head command. Both compare head failures against base failures (`orch.py:473-476`), so a pending test absent from the target is incorrectly called broken.

A PR targeting freshly fetched main, with complete approval snapshots and the locked file already on that target, passes the new control cases today. Merely having an old fork point is insufficient to reproduce the bug. The actual remote metadata and premerge logs for #1298 are unavailable in this network-blocked run; its exact target SHA is not asserted.

## Related work
At cached ref `origin/foragent/issue-1105` (`4cd9e31d8a09171cfe78eb8768b13de171d4c5a9`), #1105 retains this exclusion/filter/comparison path and unions additional protected regressions into broken. It does not repair this gap. Reuse its protection if it has merged before implementation. There is no dependency on #1105.

## Scope
One size-s issue in the existing orch lane. Use approved ownership evidence across the public merge checks, including paths introduced by newer main commits. Preserve protection for actual regressions and the existing CI, conflict and revision-freshness checks. Implementation choices remain with the lane. This is deterministic Git/approval bookkeeping; no meaning classifier, case-specific exception or agent step order is needed.

## Out of scope
Implementing #1105, changing approval policy, broadening which tests are locked, redesigning CI or runner baselines, changing notifications, and exempting arbitrary new failing tests.

## Acceptance
The locked test uses real local Git snapshots and real targeted pytest suites, with a fake tracker and isolated services. In both CI and local modes it checks: a newer main lock absent from an older PR target is nonblocking through public premerge and merge; a current-main target remains nonblocking; and a compatibility test passing separately on base and head but failing only on their merge still blocks merge. The latter case includes the pending lock too, preventing blanket acceptance-test exemptions.

## Done when
```sh
uv run pytest -q tests/acceptance/test_premerge_unmerged_locks.py
```
