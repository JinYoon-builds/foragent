# Merge groups in small steps

Ready runs with consecutive FIFO turns can share one complete local check on current main and their exact published heads, in queue order. Each member still publishes and finishes its own work. This order replaces the scope of #459 and #460; #458 remains their historical parent. It does not edit any existing issue.

## Why split this work

The merged FIFO path already prevents turn starvation, but main has no joint-check producer or consumer. Prior held attempts coupled a successful-looking result to incomplete or stale evidence, and constructed contents that could differ from sequential PR squashes. Failure and departure paths could then leave successful merges unfinished. These are evidence, publication and lifecycle gaps, not missing semantic judgment rules.

## Contracts

- The joint-check issue provides an observable complete check of the oldest consecutive ready turns against one captured current main. A complete suite preserves every member's Done when checks, the applicable passing acceptance baseline and related unit coverage. Checks and waits leave the short final merge lock available. Reuse is not needed to complete this first issue; ordinary per-PR publication remains valid there. Fail-closed evidence handling is completed before reuse becomes available.
- The reuse issue builds on the evidence safeguards and consumes that complete check only for the exact checked heads and the same combined tree that FIFO PR squash merges publish. Earlier members of that group and result-neutral moves can preserve validity. A merely adjacent commit or a neutral net diff does not establish merge provenance. These are behavior contracts, not a required evidence format or construction algorithm.
- The evidence issue builds on joint checking while ordinary publication is still available and makes absent, stale, unknown, timed-out or unreadable evidence a failure or a void group. A void group cannot authorize publication. Members resume the existing one-PR checks and policy; a changed unverified head can be held by that path. A newly protected passing test on main must not disappear from the evidence used for a later group or solo check.
- The recovery issue narrows a failed combined check to the responsible member or members, by bisect or an equivalent approach. Non-culprits continue in surviving FIFO order. Culprits retain their own normal failure/retry handling. A dead or rejected member cannot strand the queue or omit finalization of successful merges. Any remaining combined result needs passing evidence for those surviving contents.

## Shared constraints

Only locally checked runs already ready for automatic merging may share a check. The existing CI-only and approval-required one-PR routes retain their behavior. FIFO membership and run-instance identity stay authoritative; preserve existing queue bytes and skip dead owners without rewriting their state. Every runner is the only writer of its PR, issue, run record and board result. A group's successful merges complete required after-merge work once even if its planned last member dies or exits early. Preserve merge_group_max semantics: default 4, positive integers with no upper limit, invalid booleans/fractions/strings, and 1 disables grouping. The existing FIFO acceptance file remains unchanged and passing.

## Boundaries

This order contains four unattended medium issues in the existing orch lane, which owns merge/publication work today. It introduces no lane, new approval policy, new queue format contract, prescribed helper name, diagnostic partition algorithm or fixed check count for culprit search. Expected source paths in issue metadata describe ownership and likely scope; the lane decides its implementation. The first issue includes the setting integration needed for its behavior; the remaining issues depend on the earlier behavior they exercise.

## Locked checks and break-it review

Each issue owns exactly one new acceptance file under tests/acceptance. The staged copies are under order-out/tests/acceptance. They invoke real runner entry points and reuse the existing offline merge fixtures. Checks observe commands and their contents, Git publication, ownership and progress. They do not import a private group implementation or require its stored evidence schema. Existing fixture instrumentation retains its existing queue/lock observations.

- group-joint rejects per-PR-only checking, stale main, omitted member checks and incorrect size limits.
- group-reuse rejects follower rechecks on valid evidence, foreign writes, duplicate/out-of-order merges and a checked tree different from the actual squash tree. Its ancestry/revert case distinguishes ordinary ancestry merges from the real publication result.
- group-evidence rejects empty-as-success baselines, truthy treatment of unknown exit status, ignored timeout/read errors, stale heads, net-diff-only history checks and an outsider hidden in a member's observed merge. The absent-baseline case includes a healthy-group positive control so main's safe but missing group path cannot satisfy it.
- group-recovery rejects dropping all members after failure, always blaming the leader, merging a culprit, waiting for a dead owner and losing finalization when the last member leaves. It accepts equivalent culprit searches rather than locking partition sizes or counts.

## Existing issue proposals (report only)

- #458: keep as the historical parent. Link the replacement order when it is actually published, then close only after the four new issues complete. No edit is included here.
- #459: propose close as superseded/covered by group-joint, group-reuse, group-evidence and the lifecycle part of group-recovery, after replacement issues exist. No edit is included here.
- #460: propose close as superseded/covered by the culprit-isolation part of group-recovery, after replacement issues exist. No edit is included here.

## Preparation report: 2026-10-31

This is a plan and locked tests only. No implementation, issue edit, commit or push was performed. At the start, HEAD, main and origin/main all named 5ed4582094405ed256177e638e374547ebf3fc86. All tests and CLI checks used that unchanged working-tree source. At the final audit the shared main and origin/main refs had advanced externally to f097bf868297f6ac2c79d0dc3ea73ae04f84bf89; HEAD remained 5ed4582094405ed256177e638e374547ebf3fc86. This run did not move those refs and does not claim tests against the later revision. The tracked working tree was unchanged. New tests were staged outside tests/ and imported source from this working tree. All pytest runs were sequential, targeted and without xdist. Test HOME, state and temporary directories were confined to a disposable directory inside this working tree, then removed.

### Assumptions

- "On main" means the main revision captured in the supplied working directory at start (5ed4582094405ed256177e638e374547ebf3fc86). The shared branch advanced externally while work continued; the supplied worktree stayed pinned to the captured revision.
- Keep the supplied order's local-check scope; CI-only and approval-required publication keep their existing one-PR behavior. Use the supplied merge_group_max contract: default 4, positive integer without a cap, off at 1.
- The existing normal changed-head hold is a valid one-PR fallback for an unverified replacement head; it must not merge using the old head's evidence.
- Joint checking and evidence safeguards can ship before reuse; reuse is blocked by both. Recovery depends on reuse because it exercises checked groups that have partially merged. No private code interface is fixed by these dependencies.
- The remaining published contents must have passing evidence when a checked member drops out. Cleanup must complete once for successful merges even when that dropped member was supposed to finish it.

### Main failure evidence

The common staging command prefix was:

```sh
TMPDIR="$PWD/order-out/.runtime" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD/tests:$PWD/tests/acceptance" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider -p conftest
```

Each invocation added its own --basetemp inside order-out/.runtime and exactly one of the files below. The conftest plugin supplies the repository's unchanged isolation and Git fixtures because the staged files are outside tests/. Once published, the Done when commands use the normal repository tooling and automatic fixture discovery. The four file runs exited 1 for missing behavior, not collection/setup failures.

`order-out/tests/acceptance/test_group_joint_order.py`

```text
E       AssertionError: Missing joint check: ready FIFO heads were checked only separately
E       AssertionError: merge_group_max=2 must actually check two ready FIFO heads together
E               foragent.config.ConfigError: unknown configuration key: merge_group_max
FAILED order-out/tests/acceptance/test_group_joint_order.py::test_consecutive_fifo_heads_share_one_complete_check_on_current_main
FAILED order-out/tests/acceptance/test_group_joint_order.py::test_explicit_maximum_counts_the_holder_and_one_disables_grouping
FAILED order-out/tests/acceptance/test_group_joint_order.py::test_setting_accepts_unbounded_positive_integers_only
3 failed in 25.37s
```

`order-out/tests/acceptance/test_group_evidence_order.py`

```text
E       AssertionError: Positive control: a complete baseline must allow a joint check
E       AssertionError: Missing joint check: the unavailable-result scenario was never exercised
E       AssertionError: Head invalidation must exercise an actual earlier joint check
E       AssertionError: Baseline refresh must follow a real earlier joint check
E       AssertionError: Base invalidation must exercise an actual earlier joint check
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_absent_baseline_is_not_a_successful_empty_baseline
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_unusable_joint_result_falls_back_instead_of_authorizing_a_merge[unknown-exit]
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_unusable_joint_result_falls_back_instead_of_authorizing_a_merge[timeout]
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_unusable_joint_result_falls_back_instead_of_authorizing_a_merge[unreadable-result]
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_changed_head_cannot_spend_the_old_joint_pass
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_new_passing_main_test_remains_protected_after_the_group_becomes_stale
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_outsider_in_the_base_interval_invalidates_reuse[source]
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_outsider_in_the_base_interval_invalidates_reuse[reverted-source]
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_outsider_in_the_base_interval_invalidates_reuse[during-member-merge]
9 failed in 61.16s (0:01:01)
```

`order-out/tests/acceptance/test_group_reuse_order.py`

```text
E       AssertionError: Missing joint tree: no combined ready-head check was executed
E       AssertionError: The checked squash union must retain both members despite the later head's ancestry
FAILED order-out/tests/acceptance/test_group_reuse_order.py::test_exact_heads_reuse_one_pass_and_publish_the_checked_tree[only-members]
FAILED order-out/tests/acceptance/test_group_reuse_order.py::test_exact_heads_reuse_one_pass_and_publish_the_checked_tree[neutral-move]
FAILED order-out/tests/acceptance/test_group_reuse_order.py::test_later_head_containing_and_reverting_a_peer_still_checks_the_squash_result
3 failed in 30.88s
```

`order-out/tests/acceptance/test_group_recovery_order.py`

```text
E       AssertionError: Culprit isolation must start from the failed combined check
E       AssertionError: Exercise a failing joint check on a docs-only move
E       AssertionError: Death recovery must exercise an actual checked group
E       AssertionError: Finalization must cover an actual checked group
E       AssertionError: Missing group admission: the leader-loss scenario was never exercised
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_failed_joint_check_isolates_culprits_and_keeps_survivors_moving[leader]
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_failed_joint_check_isolates_culprits_and_keeps_survivors_moving[tail]
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_failed_joint_check_isolates_culprits_and_keeps_survivors_moving[two-culprits]
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_failed_joint_evidence_cannot_be_discarded_as_a_neutral_docs_move
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_dead_member_is_skipped_and_successful_merges_are_finalized[middle]
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_dead_member_is_skipped_and_successful_merges_are_finalized[last]
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_changed_last_head_cannot_lose_finalization_for_prior_merges
FAILED order-out/tests/acceptance/test_group_recovery_order.py::test_dead_leader_with_no_result_does_not_strand_the_remaining_turns
8 failed in 95.40s (0:01:35)
```

The second-phase run IDs in the off=1 and absent-baseline cases were moved away from existing fake PR numbers. Only those two changed cases were rerun, with the same expected missing-feature failures:

```text
FAILED order-out/tests/acceptance/test_group_joint_order.py::test_explicit_maximum_counts_the_holder_and_one_disables_grouping
FAILED order-out/tests/acceptance/test_group_evidence_order.py::test_absent_baseline_is_not_a_successful_empty_baseline
2 failed in 19.08s
```

The unchanged FIFO file was run separately using the same interpreter and source path, without -p conftest (its fixture discovery is automatic):

```sh
TMPDIR="$PWD/order-out/.runtime" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD/tests:$PWD/tests/acceptance" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp="$PWD/order-out/.runtime/fifo" tests/acceptance/test_merge_turn.py
```

```text
....                                                                     [100%]
4 passed in 21.56s
```

No implementation or mutation-test run is claimed. The break-it cases above describe what each locked check rejects when a lane implements the feature.

### Dry-run evidence

The required native command was executed with HOME and XDG directories isolated inside the working tree and local src on PYTHONPATH:

```sh
fa order publish --dry-run order-out/plan.json
```

It exited 1 before a GitHub-backed preview, with this actual output:

```text
foragent: To get started with GitHub CLI, please run: gh auth login Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
```

Network was blocked, and no authentication or external issue writes were attempted. For an offline preview, the same local CLI entry point was invoked with its existing FakeTracker seeded from the provided #458/#459/#460 snapshots in place of GhTracker. The publisher, plan parser, config and schema validation were real. No publisher logic was replaced. This offline CLI dry-run exited 0; it validates the plan without claiming a successful live GitHub lookup. Its output was:

```text
Plan (dry run): Merge groups in small steps (replaces #459/#460)
Files: docs/specs/2026-10-31-merge-groups-split.md, tests/acceptance/test_group_evidence_order.py, tests/acceptance/test_group_joint_order.py, tests/acceptance/test_group_recovery_order.py, tests/acceptance/test_group_reuse_order.py, tests/acceptance/test_merge_turn.py

lane:orch
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| group-joint | Check consecutive ready FIFO heads together once | m | — | no | no |
| group-evidence | Fall back when joint evidence cannot prove the merge | m | group-joint | no | no |
| group-reuse | Let owners publish exactly the jointly checked result | m | group-evidence | no | no |
| group-recovery | Keep surviving FIFO runs moving after group failure or departure | m | group-reuse | no | no |
```

The unchanged FIFO file appears in the prospective publish list because it is a Done when regression check; no changed copy of it was written. Local schema validation and Python AST parsing of all four locked files passed. The four acceptance_files values exactly match the staged files. There are no statements, no existing-issue edits, no new lanes and no sizes above m.
