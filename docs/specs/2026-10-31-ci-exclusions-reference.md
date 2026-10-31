# Compare against the exclusions CI recorded

## Goal

Use the CI-recorded exclusion list as the reference for a completed acceptance comparison in both directions: today's list may gain entries, lose entries, or change both ways without invalidating otherwise usable evidence. Apply the same historical reference to the report and its recorded scope. Tests excluded at CI time are not protected by that comparison. A test CI measured that passed on its base and fails or is deselected on its head still blocks, even if it is excluded today. Identical lists keep existing behavior, and every other evidence integrity check remains in force.

## Context and root cause

The supplied incident record identifies three occurrences on 2026-10-31: #1054, #1046 and #993. The actual rejection was `report.excluded: observed [...], expected a subset of [...]`. Orders merged after CI ran, so their locked tests left today's exclusions. The recorded list therefore contained paths absent from today's list; new orders could also add paths in the other direction.

Main already permits the recorded list to be a subset of today's list, since #567. A growth-only example cannot reproduce these incidents. The root cause is treating a historical measurement's exclusions as constrained by a later lock population. Both `report.excluded` and `report.scope.excluded` reject recorded paths no longer excluded today. A second manifestation uses today's exclusions to waive a regression in a test CI actually measured. These are mechanical evidence/reference defects, not missing semantic judgment rules.

The CI-recorded list is the comparison's reference regardless of later additions or removals. Tests CI excluded have no measured passing status to protect in that comparison. Tests CI did measure keep their protection when today's locks change.

Approved issue #1109 owns stale-base/producer recovery and diagnostics. This order changes the temporal reference for exclusions and preserves those behaviors and all other integrity checks. The lane chooses the implementation.

## Public acceptance contract

The existing public `foragent.ci_mode.comparison` entry point and its established run/artifact/report evidence are the contract used by `tests/acceptance/test_ci_mode.py` and `tests/acceptance/test_night_ci_evidence.py`. Usable evidence returns the lost passing test IDs and comparison duration. A nonempty lost-ID result blocks under the existing runner contract; an evidence rejection is not a successful comparison.

Lock these three cases:

1. CI excluded a locked file whose order has since merged, so the current exclusions no longer contain it. Test both a strictly shrunken current list and a list that also gains a new locked file. Both recorded exclusion fields retain the old list, and the comparison remains usable with no lost passing IDs. Neither the file excluded by CI nor the later-added locked file was measured, so neither creates a blocker or gains retroactive protection. Independently exercise the scope boundary with the report reference already historical: fixing only the report constraint must not hide a remaining scope subset constraint.
2. A test not excluded when CI measured it passed on the CI base and fails or is deselected on the CI head. It remains exactly the lost passing ID even when today's exclusion list includes that file. Later locking cannot waive the regression; an unrelated test already failing on the CI base stays nonblocking.
3. With identical recorded/current exclusions, healthy evidence returns no lost IDs and an existing regression returns its exact lost ID. Base failures remain nonblocking and duration retains its existing meaning.

The locked file reuses the existing trusted two-shard report fake and CI-mode test IDs. It calls the public comparison without replacing production logic. An additional integrity control keeps contradictory report/scope exclusion lists unusable, even when each list is separately a subset of today's exclusions.

## Integrity and scope

Keep repository and producer trust, base/head identity, workflow identity, non-exclusion scope, exclusion field shapes and report/scope consistency, complete base/head suites, shard coverage and uniqueness. A report that contains a test it says was excluded remains invalid. Keep existing field diagnostics for contradictory recorded evidence. Ordinary green CI checks cannot substitute for comparison evidence. Local comparison behavior, freshness recovery, diagnostics, wait/repair budgets, workflow production, approval integrity and lane definitions remain outside this change.

## Lane and assumptions

Use size s and implicit lane main. No path in the supplied `.foragent.yml` or the verified main snapshot's configuration matches `src/foragent/ci_mode.py`; the orch patterns cover other files. No new lane or fixed skill is needed. There is no new dependency on #1109: the supplied source already contains its comparison diagnostics and stale-evidence handling.

The standalone source stays under `order-out/tests/acceptance/` and is embedded verbatim in this plan at its eventual `tests/acceptance/` path. No existing locked test is amended. The independent scope case uses the public evidence contract's separate exclusion and expected-scope inputs to expose each constraint without prescribing a production call sequence.

## Verification on main

Verified main snapshot `459331cf0e285b1be7f44f2dc7db6d1adfa5d827`. Because branch switching is prohibited, exported that exact commit's source, test support, project file and configuration into a temporary directory inside this working directory. Ran against the exported main source and fakes via PYTHONPATH, with the existing cached Python environment, UV_OFFLINE=1, UV_NO_SYNC=1, disabled bytecode/pytest caches, and all temporary data inside the working directory. No implementation file was changed.

```text
uv run pytest -q order-out/tests/acceptance/test_ci_exclusions_after_ci.py
E   foragent.ci_mode.UnusableComparison: report.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py"]
E   foragent.ci_mode.UnusableComparison: Unusable acceptance shard: report.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py"]
E   foragent.ci_mode.UnusableComparison: report.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py", "tests/acceptance/test_order_added_after_ci.py"]
E   foragent.ci_mode.UnusableComparison: Unusable acceptance shard: report.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py", "tests/acceptance/test_order_added_after_ci.py"]
E   foragent.ci_mode.UnusableComparison: report.scope.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py"]
E   foragent.ci_mode.UnusableComparison: Unusable acceptance shard: report.scope.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py"]
E   foragent.ci_mode.UnusableComparison: report.scope.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py", "tests/acceptance/test_order_added_after_ci.py"]
E   foragent.ci_mode.UnusableComparison: Unusable acceptance shard: report.scope.excluded: observed ["tests/acceptance/test_feature.py", "tests/acceptance/test_queued.py"], expected a subset of ["tests/acceptance/test_feature.py", "tests/acceptance/test_order_added_after_ci.py"]
E   AssertionError: A test measured by CI lost its passing status; later locks cannot waive it
E   assert [] == ['tests/accep...y::test_keep']
E     
E     Right contains one more item: 'tests/acceptance/test_keep.py::test_keep'
E     Use -v to get more diff
FAILED order-out/tests/acceptance/test_ci_exclusions_after_ci.py::test_ci_exclusions_removed_from_current_locks_leave_comparison_usable_and_unblocked[both-fields-current-shrank]
FAILED order-out/tests/acceptance/test_ci_exclusions_after_ci.py::test_ci_exclusions_removed_from_current_locks_leave_comparison_usable_and_unblocked[both-fields-current-removed-and-added]
FAILED order-out/tests/acceptance/test_ci_exclusions_after_ci.py::test_ci_exclusions_removed_from_current_locks_leave_comparison_usable_and_unblocked[scope-only-current-shrank]
FAILED order-out/tests/acceptance/test_ci_exclusions_after_ci.py::test_ci_exclusions_removed_from_current_locks_leave_comparison_usable_and_unblocked[scope-only-current-removed-and-added]
FAILED order-out/tests/acceptance/test_ci_exclusions_after_ci.py::test_test_not_excluded_at_ci_still_blocks_even_if_it_is_locked_now[failed]
FAILED order-out/tests/acceptance/test_ci_exclusions_after_ci.py::test_test_not_excluded_at_ci_still_blocks_even_if_it_is_locked_now[deselected]
6 failed, 3 passed in 0.53s
```

Case 1 fails for the actual subset reason in both directions of current-list change: two cases name `report.excluded`, and the two independent scope cases name `report.scope.excluded`. Case 2 has two lost-ID assertion failures. Both identical-list controls and the report/scope consistency control pass. There are no collection or setup errors.

Existing integrity targets were run unchanged from that same main export:

```text
uv run pytest -q order-out/.verify/main/tests/acceptance/test_night_ci_evidence.py::test_shard_rejection_names_the_field_and_expected_and_observed_values order-out/.verify/main/tests/acceptance/test_night_ci_evidence.py::test_multiple_invalid_fields_report_the_first_encountered_check order-out/.verify/main/tests/acceptance/test_night_ci_evidence.py::test_cross_shard_identity_failures_also_show_field_and_both_values order-out/.verify/main/tests/test_ci_mode_unit.py::test_excluded_test_in_report_names_the_test_and_exclusions order-out/.verify/main/tests/test_ci_mode_unit.py::test_rejects_unusable_shards
33 passed in 0.79s
```

Break-it check: six in-memory wrong-behavior substitutions were each detected: retain the report subset restriction, retain only the scope subset restriction, waive measured regressions, block healthy evidence, report a regression twice, and accept contradictory recorded scope. No production source was changed for this experiment. The real main run independently reproduces both subset restrictions and the regression waiver.

All Done when commands remain targeted and use `uv run pytest`. No full-suite run, pytest-xdist, implementation, network publication or git mutation belongs to this preparation.

## Offline publication validation

Ran `fa order publish --dry-run order-out/plan.json`: exit 0. The actual CLI used the exported main source, an empty offline FakeTracker and temporary state inside the working directory. The preview contains one size-s issue on lane main, unapproved and noninteractive, with the new acceptance file and unchanged existing acceptance files selected by Done when. This verifies local schema/publication preview; no live issue identity or shared-lock ownership was checked, and no network record was read or changed.
