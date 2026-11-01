# Request completion records and meaningful role-signal changes

Source: Jin's choice-free request, 2026-11-01, via the PM. This is a plan and locked checks only.

## Shared outcome
The orchestrator can finish a request with its answer and, for a role proposal, its explicit decision in one command. The scorecard reflects those decisions. Answered role signals stay quiet until relevant evidence changes. Neither result requires an extra reminder in an agent prompt.

## Evidence and root causes
- Reported incident: 109 done requests have no answer; accepted/applied architect proposals r78, r81, r84-r86, r88, r98-r106 do not appear in the accepted-proposal total. These production totals are supplied by the PM, not remeasured in this offline run.
- `src/foragent/commands/_orch_box.py` exposes `request-status` with only a status and optional note. `src/foragent/orch_inbox.py::set_request_status` persists status and notes without a completion answer. `src/foragent/orch_roles.py::decide` records a role-proposal event and closes the request but accepts no answer and leaves the decision out of inbox JSON. These separate, incomplete completion paths make a reminder to run another command insufficient.
- `src/foragent/orch_roles.py::scorecard` already counts explicit accepted role-proposal records once per role/request. `src/foragent/orch_scorecard.py::command` adds those role totals to the public scorecard. Closing a request alone supplies no such record; done must never be interpreted as acceptance.
- `src/foragent/orch_role_raise.py::fingerprint` retains entire raw kind-number payloads and `raise_due` compares them for not-now. Counts within the same affected file/issue/lane set therefore re-arm an unchanged concern. For called, `raise_due` still uses the old elapsed-time cooldown, so time alone re-arms it.
- `src/foragent/orch_facts_kinds.py` documents the measured facts: hot files are files in at least three overlapping runs, oversized files exceed the line limit, repeated holds concern issues meeting the repeat condition, and idle-lane facts name idle lanes and lanes meeting the backlog condition. The required distinction is factual membership in each condition; deciding whether to call or defer a role remains the orchestrator's judgment.

## Issue boundaries and ownership
Both issues use the existing `roles` lane: `.foragent.yml` assigns `orch_inbox.py`, `orch_role*.py`, and `orch_facts*.py` there. The first issue also follows the existing completion CLI and scorecard integration. Both are size s, unattended, and independently verifiable; both have `blocked_by: []`. No new lanes or fixed skills. Lane agents select applicable tool/review/domain skills from the approved catalog.

## Observable contracts used by the locked checks
- Existing `fa orch request-status ID done` takes `--answer TEXT` for an ordinary request. Existing `fa orch role-decision ID accepted|rejected --role ROLE` takes `--answer TEXT` in that same invocation. These are the smallest assumed public CLI extensions; no storage layout, helper, event-writing order, or transaction mechanism is prescribed.
- After successful completion, `fa orch inbox --json` exposes status done and the exact supplied `answer`. A proposal also exposes its role and boolean `accepted`. Scorecard JSON retains the existing `window.today.roles.ROLE.accepted_proposals` contract. Accepted contributes one, rejected and ordinary requests contribute zero; repeating a close cannot double-count it.
- Closing without an answer may be refused with the request and decision count unchanged, or may record an explicit nonempty explanation of the empty answer in inbox JSON (`answer` or `empty_answer_reason`). The lane chooses the behavior.
- After either not-now or called, counts drifting inside the same affected sets and passage of time alone produce zero new requests. A new affected file, a newly repeating issue, or a new idle lane produces exactly one new request on the next tick, including within the old cooldown. Open requests are not duplicated; an answer to the new evidence starts the same quiet behavior again. Gate restarts retain it.

## Scope assumptions
This order covers future completion records and evidence-based signal re-raising. It does not infer or manufacture historical answers or proposal decisions. Signal tests cover hot_overlap_files and idle_lanes_with_backlog as requested, plus files_over_limit and repeated_design_holds from the same reported incident. The idle-lane binding constraint stays unchanged in the count-drift tests; a genuinely changed constraint is still relevant evidence. There is no shared interface dependency between the issues.

## QA reconciliation of the older locked contracts (#1337)
Classification: this conflict is a planning judgment problem, not a tool defect. The first draft documented contradictory locked expectations without delivering corrected checks. The missing context was explicit ownership and authority to reconcile those locked tests; the QA instruction now confirms the orchestrator owns them and supplies that authority. The correction is a complete, executable acceptance contract in this same issue. No runtime script gains a judgment rule, classifier, or new agent step order.

| Existing case | Judgment about its facts | Contract in this order |
| --- | --- | --- |
| `test_changed_facts_raise_exactly_one_new_request[binding-changes]` | Meaningful: the binding changes from cap to quota. | Keep re-raising exactly once on the next tick. |
| `test_changed_facts_raise_exactly_one_new_request[idle-lanes-change]` | Meaningful: skills becomes a newly idle lane. | Keep re-raising exactly once on the next tick. |
| `test_changed_facts_raise_exactly_one_new_request[ready-work-changes]` | Count drift: orch has 4 then 5 ready issues; the idle and backlog lane sets and binding are unchanged. | Rewrite to zero re-raises, including beyond the old cooldown. Keep the function name and parameter id. |
| `test_an_answered_signal_waits_for_the_cooldown_while_it_stays_met` | No new evidence: a called path signal is still about the same file; only time passes. | Rewrite to stay quiet after 12 hours; a subsequently merged, newly matching file must still re-raise once. Keep the historical function name. |
| `test_a_signal_that_clears_can_be_raised_again_inside_the_cooldown` | Meaningful: the condition becomes unmet, then met again. | Keep the existing re-raise expectation unchanged. |

The two corrected full files are carried only by signal-meaningful-change.acceptance_files, with their repository paths added to that issue's files. Its Done when now runs the new file and both corrected older files. The request-answer-record issue and its acceptance file are unchanged. No implementation or source-tree test file is edited in this preparation run.

## Break-it review
Request checks fail if completion drops an answer, drops/reverses a decision, counts rejections or ordinary requests, affects a neighboring request, counts the same close twice, or silently completes without an answer. Signal checks fail for raw-count comparison, timer-only suppression, permanent muting, missing a newly affected entity, duplicate raises, or suppression lost when the store is reopened. Tests write no answer/decision/suppression records directly and fake only infrastructure or measured facts. No implementation or mutation-testing patch was applied.

## Baseline verification on main
At the baseline run, HEAD and local main both equaled `cc7ffb9e9bac2cdd7c111f2cd17e802f5a0b8117` (detached working tree; no checkout command used). No network was used. Tests ran separately using the existing installed environment, local source imports, the repository's offline conftest fakes, and all temporary files under order-out/.verification.

Common environment: UV_PROJECT_ENVIRONMENT=/Users/apple/Dev/foragent/.venv, UV_NO_SYNC=1, UV_OFFLINE=1, UV_CACHE_DIR=$PWD/order-out/.verification/uv-cache, TMPDIR=$PWD/order-out/.verification/tmp, PYTHONDONTWRITEBYTECODE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONPATH=$PWD/src:$PWD/tests:$PWD/tests/acceptance.

```sh
uv run pytest -q order-out/tests/acceptance/test_orch_request_answer_record.py -p conftest --basetemp=order-out/.verification/request -p no:cacheprovider
```

Actual failure lines:
```text
E       AssertionError: One closing command must accept the answer: usage: fa [-h] [--version]
E         fa: error: unrecognized arguments: --answer Prepared two independent issues and their locked acceptance checks.
E       AssertionError: One closing command must take both answer and decision: usage: fa [-h] [--version]
E         fa: error: unrecognized arguments: --answer Applied the proposed split in the published order.
E         fa: error: unrecognized arguments: --answer Kept the module together because its callers need one transaction.
E           AssertionError: A successful close must never silently leave no answer; record an explicit empty reason
FAILED order-out/tests/acceptance/test_orch_request_answer_record.py::test_completion_persists_the_answer_without_counting_an_ordinary_request
FAILED order-out/tests/acceptance/test_orch_request_answer_record.py::test_one_proposal_closing_command_records_answer_decision_and_scorecard_once[accepted]
FAILED order-out/tests/acceptance/test_orch_request_answer_record.py::test_one_proposal_closing_command_records_answer_decision_and_scorecard_once[rejected]
FAILED order-out/tests/acceptance/test_orch_request_answer_record.py::test_a_bare_close_is_refused_or_records_an_explicit_explanation[ordinary]
FAILED order-out/tests/acceptance/test_orch_request_answer_record.py::test_a_bare_close_is_refused_or_records_an_explicit_explanation[accepted-proposal]
FAILED order-out/tests/acceptance/test_orch_request_answer_record.py::test_a_bare_close_is_refused_or_records_an_explicit_explanation[rejected-proposal]
6 failed in 1.64s
```

```sh
uv run pytest -q order-out/tests/acceptance/test_role_signal_meaningful_change.py -p conftest --basetemp=order-out/.verification/signal -p no:cacheprovider
```

Actual failure lines:
```text
E           AssertionError: hot_overlap_files: not-now must stay quiet when only counts/order within the same affected set change
E           assert ['r1', 'r2'] == ['r1']
E           AssertionError: idle_lanes_with_backlog: not-now must stay quiet when only counts/order within the same affected set change
E           AssertionError: files_over_limit: not-now must stay quiet when only counts/order within the same affected set change
E           AssertionError: repeated_design_holds: not-now must stay quiet when only counts/order within the same affected set change
E           AssertionError: hot_overlap_files: called must stay quiet when only counts/order within the same affected set change
E           AssertionError: idle_lanes_with_backlog: called must stay quiet when only counts/order within the same affected set change
E           AssertionError: files_over_limit: called must stay quiet when only counts/order within the same affected set change
E           AssertionError: repeated_design_holds: called must stay quiet when only counts/order within the same affected set change
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-hot-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-idle-lanes]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-oversized-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-repeated-holds]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-hot-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-idle-lanes]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-oversized-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-repeated-holds]
8 failed in 4.58s
```

The corresponding Done when commands use the eventual published tests/acceptance paths. The files under order-out are byte-for-byte copies of their acceptance_files entries. No src/ or tests/ files are changed by this order preparation.

## Publish dry-run verification
Ran the public CLI equivalent of `fa order publish --dry-run order-out/plan.json` through `foragent.cli.main` with the real local configuration, schema validator, and preview renderer. Only the GitHub boundary used `FakeTracker` with an empty issue list. Subprocess and socket access were blocked. Result: exit 0; exactly the two size-s roles issues, neither blocked, neither approved nor interactive. No issues, acceptance files under tests/, or spec under docs/ were written. This verifies offline publication shape, not live GitHub state.


## QA baseline verification on main
Pinned main at `a1d5fedccbbac2380efac23e233dab0c6f9e180b` and exported its complete tracked tree using `git archive` into `order-out/.verification/main`. No checkout command was used. Ran the corrected test copies from that snapshot's order-out/tests/acceptance with the snapshot's src/, tests/, tests/acceptance and conftest. This tests the actual main snapshot, rather than relying on the older detached working-tree source. The snapshot and all other verification temporary files remain inside the supplied working directory and are removed after verification.

Environment: UV_PROJECT_ENVIRONMENT=/Users/apple/Dev/foragent/.venv, UV_NO_SYNC=1, UV_OFFLINE=1, UV_CACHE_DIR=$PWD/../uv-cache, TMPDIR=$PWD/../tmp, PYTHONDONTWRITEBYTECODE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONPATH=$PWD/src:$PWD/tests:$PWD/tests/acceptance. Here $PWD is the archived main root. Each file ran separately; no xdist.

```sh
uv run pytest -q order-out/tests/acceptance/test_role_signal_not_now.py -p conftest --basetemp="$PWD/../not-now" -p no:cacheprovider
```

Actual result: pytest exit 1 (expected baseline failures).
```text
E       AssertionError: Changed binding or a new idle lane must re-raise once; count drift in the same backlog lane must stay quiet
E       assert 1 == 0
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_changed_facts_raise_exactly_one_new_request[ready-work-changes]
1 failed, 5 passed in 5.78s
```

```sh
uv run pytest -q order-out/tests/acceptance/test_role_signal_raise.py -p conftest --basetemp="$PWD/../raise" -p no:cacheprovider
```

Actual result: pytest exit 1 (expected baseline failures).
```text
E       AssertionError: A called signal must stay quiet after the old cooldown when no new evidence appears
FAILED order-out/tests/acceptance/test_role_signal_raise.py::test_an_answered_signal_waits_for_the_cooldown_while_it_stays_met
1 failed, 4 passed in 1.47s
```

```sh
uv run pytest -q order-out/tests/acceptance/test_role_signal_meaningful_change.py -p conftest --basetemp="$PWD/../new-signal" -p no:cacheprovider
```

Actual result: pytest exit 1 (expected baseline failures).
```text
E           AssertionError: hot_overlap_files: not-now must stay quiet when only counts/order within the same affected set change
E           assert ['r1', 'r2'] == ['r1']
E           AssertionError: idle_lanes_with_backlog: not-now must stay quiet when only counts/order within the same affected set change
E           AssertionError: files_over_limit: not-now must stay quiet when only counts/order within the same affected set change
E           AssertionError: repeated_design_holds: not-now must stay quiet when only counts/order within the same affected set change
E           AssertionError: hot_overlap_files: called must stay quiet when only counts/order within the same affected set change
E           AssertionError: idle_lanes_with_backlog: called must stay quiet when only counts/order within the same affected set change
E           AssertionError: files_over_limit: called must stay quiet when only counts/order within the same affected set change
E           AssertionError: repeated_design_holds: called must stay quiet when only counts/order within the same affected set change
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-hot-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-idle-lanes]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-oversized-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[not-now-repeated-holds]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-hot-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-idle-lanes]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-oversized-files]
FAILED order-out/tests/acceptance/test_role_signal_meaningful_change.py::test_count_drift_stays_quiet_and_a_new_affected_entity_raises_once[called-repeated-holds]
8 failed in 4.94s
```

The corrected ready-work-changes case fails only because main raises one request for same-lane count drift (expected zero). The corrected called-signal case fails only because main re-raises after time alone (expected none). The five and four passing older cases retain meaningful re-raises and existing non-conflicting behavior; no import/setup/infrastructure failure occurred. The eight new cases fail for count/order drift or the old called timer, as in the original baseline. No implementation pass is claimed.

QA dry-run result: `fa order publish --dry-run order-out/plan.json` ran through the public CLI on the archived main with the real local configuration, schema validator and renderer, and only the GitHub boundary replaced by the repository's empty FakeTracker. Exit 0. The preview contains the original two independent roles/s issues and all four acceptance paths, including both corrected older files only under signal-meaningful-change. Subprocess/socket access was blocked; no issues or test files were published. This is an offline shape check, not a check of live GitHub issue state.
