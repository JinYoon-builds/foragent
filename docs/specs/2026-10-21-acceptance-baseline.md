# Already-passing acceptance tests must keep passing

Implements D53.

### acceptance-baseline — D53 baseline of passing acceptance tests (size m)
Goal: Implements D53 (docs/decisions.md).
(1) Before the first implementation attempt, when a test command is known (`config.test_command`, else the first of `order.detect_test_commands(worktree)`) and it is pytest-based, the runner runs it on the fresh worktree (target branch, before any change) against the whole acceptance directory with `--junitxml=<state dir file>` and records the node ids that pass: the baseline. Non-pytest commands skip the baseline and log why.
(2) The implementer prompt says those acceptance tests already pass and must keep passing, listing them (ids), e.g. `These acceptance tests already pass on the base and must keep passing: ...`. It must contain the phrase `already pass`.
(3) In the verification step after each attempt, the runner re-runs the same command for the baseline node ids only. Any failure is a check failure: its output goes back to the implementer through the existing retry path (same attempt budget as a failing Done when). Acceptance tests that were failing on the base (other issues' unimplemented tests) are never run or reported.
(4) Record the baseline count and result in the run log.
Out of scope: Changing CI; non-pytest frameworks beyond skipping.
Test checks: tests/acceptance/test_acceptance_baseline.py
