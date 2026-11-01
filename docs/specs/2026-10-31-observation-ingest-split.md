# Split #1025: observation ingest first

## Root cause and split

The supplied reviews identify one recurring failure class: not every GitHub read records its observations, stale presence or absence can replace newer facts, and local write acknowledgments can acquire a later time than the intent they acknowledge. Absence can lose its ordering evidence, and confirmed writes can remain pending when their effect matches the prior baseline. These are shared observation/provenance failures beneath decision completion.

Create observation-ingest on the existing board lane, size m. It owns the ordered observation behaviour for issue lists, sub-issue reads, single-issue reads and refreshes, including closed-issue reads and absence. Edit #1025 to retain decision links, completion from published work, heading cleanup and agent context. Its Goal explicitly says: Builds on the observation-ingest issue; starts after it merges. Edits cannot express blocked_by, so this dependency is stated in the Goal rather than an unsupported field.

## Behaviour shared with #1025

Observation order uses read-start time. An older observation has no effect on observed facts, provenance, pending writes or baselines. A newer observation replaces all observed issue fields together. Missing-issue and complete-listing absence observations retain enough ordering evidence to reject stale deletion and stale resurrection; omission from a filtered or partial listing alone does not establish absence.

Local writes are overlays whose provenance is their start time, including through delayed acknowledgment. An observation at or after that start retires an overlay whether it confirms or contradicts the write, including confirmation of an effect already present in the baseline. Observations still advance while overlays exist. Every read path must produce the same result for late older reads, delayed contradicted acknowledgments, reopening after completed, and completed-to-not_planned changes on an already closed issue.

#1025 consumes those facts. It retains its existing published decision-tracking contract in docs/specs/2026-10-31-decisions-stall-overlap-pstack.md, section 1, except that observation ingestion and write ordering now belong solely to observation-ingest. Optional per-issue/edit decisions lists publish or replace one canonical marker. Omission preserves explicit links; an explicit empty list clears them and overrides legacy fallback. Published legacy exact references prove association, not semantic implementation.

The offline read-only decisions view keeps its specified JSON output and observation-time provenance. A decision is done only when all linked work has qualifying completion evidence; cancelled, reopened, missing and unknown work does not qualify. A currently reopened issue remains in progress despite an older merge. Preserve the distinction between unavailable ledger input and a successfully read empty ledger. The command does not fetch GitHub or edit the ledger.

Preserve the current Goal's heading-tail rule: remove status words only and move any remaining note into the decision body unchanged. Retain derived-status documentation and the nothing-ordered context for planning and the orchestrator retrospective. The obsolete preread-only formatting directive is removed; implementation choices remain with the lane under D98.

## Preparation and completion

Both entries use today's acceptance_files publication route, with no statements fields. Issue A carries the full locked tests/acceptance/test_observation_ingest.py text and keeps Done when: uv run pytest tests/acceptance/test_observation_ingest.py -q. This is a test-and-order preparation change only; no implementation is supplied.

#1025 retains uv run pytest tests/acceptance/test_decision_tracking.py -q. Its locked file was read from main and compared with the working-tree copy. It contains no observation-response ordering assertions to remove, so its acceptance_files is empty and every test function name remains unchanged. The existing reopened-versus-old-merge assertions are derived completion checks owned by #1025; the repeated observed_at read checks that the offline command does not renew provenance, not competing observation order.

The observation suite uses existing FakeTracker and offline_local_cli acceptance support, the public RecordingTracker read/write/refresh APIs and fa gate status --json / --live. Public StateStore APIs seed fixtures and inspect persisted facts; no private production helpers are imported. The first three matrix routes are issue-list, sub-issue and single-issue; status-refresh adds the real bulk CLI integration. Each route covers late older responses, delayed contradicted body/label/state acknowledgments, reopen after completed, closed completed-to-not_planned and delayed missing responses. Other cases cover pending confirmation matching baseline (including equal start times), stale resurrection after complete absence, filtered omissions, raw child reads during bulk refresh and light closed-issue refreshes.

Assumptions: a sub-issue omission is partial evidence and does not prove that the child issue is absent. Therefore delayed-missing cases start an authoritative single-issue 404 and interleave a newer present observation through each matrix route. They also demand deletion for a genuinely newer 404, rejecting implementations that ignore all missing responses. Existing completed records are seeded through the public store API, as in #1025's fixture, so tests reach the reopen/closure-reason transition without depending on already-implemented closure ingestion. Public local_issues records and ordinary gate JSON expose the outcomes; no new private store layout or helper is prescribed.

No new lane, implementation layout, private name, meaning classifier, approval change or mandatory agent workflow is prescribed. Files are expected scope estimates. This local plan is for dry-run review only.

## Break-it review (before implementation)

Baseline: main and HEAD both 263b5d6e06caae4a6bc6e7c5aebb36bd8b126a6b. Targeted run: 47 failed in 1.45s; exit 1. All 47 failures are behaviour AssertionErrors, with no collection/import/fixture errors. No mutant implementations were run; the rejection mapping below is the test-design review.

Wrong implementations rejected (each parametrized instance inherits its row's counterexample):

| Test family | Wrong implementation rejected |
| --- | --- |
| late_older_read | Apply responses by completion time or remote updated_at; update only some fields; bypass list/child ingestion; protect visible fields but corrupt overlays, provenance or later confirmation. |
| delayed_write_ack [update_issue/add_labels/close_issue] | Stamp body, labels or state intent at acknowledgment time; reapply the obsolete overlay after a newer contradictory read. |
| reopen_after_completed | Preserve completed closure evidence or treat completion as permanent. |
| completed_to_not_planned | Skip already-closed rows or update state without replacing state_reason. |
| delayed_missing_response | Unconditionally delete on a late 404; erase newer pending writes; or ignore all 404s (the later authoritative 404 must delete). |
| confirmation_matching_prior_baseline [add_labels/remove_labels/update_issue/close_issue] | Retire only if the observation differs from the old baseline; skip fresh observed fields while an overlay exists; require strictly later rather than equal read start. |
| complete_absence | Forget absence provenance and allow stale resurrection; treat filtered omission as complete absence; permanently suppress all later present observations. |
| status_refresh_ingests_sub_issue | Fix only the public wrappers while bulk refresh discards raw sub-issue observations. |
| light_closed_issue_refresh | Read closed issues only for open-to-closed transitions and ignore closure-reason changes. |

Exact per-case failure lines from main (artifact path retained, since tests/ was not modified):

```text
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_late_older_read_cannot_replace_newer_facts_or_provenance[issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_late_older_read_cannot_replace_newer_facts_or_provenance[sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_late_older_read_cannot_replace_newer_facts_or_provenance[single-issue]
E   AssertionError: An older read must change no facts or provenance
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_late_older_read_cannot_replace_newer_facts_or_provenance[status-refresh]
E   AssertionError: An older read must change no facts or provenance
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[update_issue-issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[update_issue-sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[update_issue-single-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[update_issue-status-refresh]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[add_labels-issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[add_labels-sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[add_labels-single-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[add_labels-status-refresh]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[close_issue-issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[close_issue-sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[close_issue-single-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read[close_issue-status-refresh]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_reopen_after_completed_clears_old_closure_evidence[issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_reopen_after_completed_clears_old_closure_evidence[sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_reopen_after_completed_clears_old_closure_evidence[single-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_reopen_after_completed_clears_old_closure_evidence[status-refresh]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_completed_to_not_planned_on_already_closed_issue[issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_completed_to_not_planned_on_already_closed_issue[sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_completed_to_not_planned_on_already_closed_issue[single-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_completed_to_not_planned_on_already_closed_issue[status-refresh]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_missing_response_preserves_newer_read_and_pending_write[issue-list]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_missing_response_preserves_newer_read_and_pending_write[sub-issue]
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_missing_response_preserves_newer_read_and_pending_write[single-issue]
E   AssertionError: A newer confirmed missing issue must disappear locally
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_delayed_missing_response_preserves_newer_read_and_pending_write[status-refresh]
E   AssertionError: A newer confirmed missing issue must disappear locally
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[add_labels-payload0-issue-list]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[add_labels-payload0-sub-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[add_labels-payload0-single-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[add_labels-payload0-status-refresh]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[remove_labels-payload1-issue-list]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[remove_labels-payload1-sub-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[remove_labels-payload1-single-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[remove_labels-payload1-status-refresh]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[update_issue-payload2-issue-list]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[update_issue-payload2-sub-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[update_issue-payload2-single-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[update_issue-payload2-status-refresh]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[close_issue-payload3-issue-list]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[close_issue-payload3-sub-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[close_issue-payload3-single-issue]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_confirmation_matching_prior_baseline_retires_pending_write[close_issue-payload3-status-refresh]
E   AssertionError: A newer confirming observation must retire even a no-op write
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_complete_absence_rejects_stale_resurrection_but_filtered_omission_is_not_absence
E   AssertionError: An older listing must not resurrect an issue after newer absence
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_status_refresh_ingests_sub_issue_facts_read_after_the_listing
E   AssertionError: The newest observation must replace every issue field together
FAILED order-out/tests/acceptance/test_observation_ingest.py::test_light_closed_issue_refresh_replaces_reason_on_an_already_closed_issue
E   AssertionError: The newest observation must replace every issue field together
47 failed in 1.45s
```

Retained #1025 test functions (unchanged): `test_publication_retains_decision_marker_and_republication_replaces_it`, `test_invalid_decision_ids_are_rejected_before_publication`, `test_decisions_command_combines_markers_and_published_legacy_specs_and_tracks_state`, `test_missing_ledger_is_distinguishable_from_a_readable_empty_ledger`, `test_repository_decision_headings_have_no_hand_written_unimplemented_status`.

## Verification commands and publish preview

The test artifact stays under order-out because src/ and tests/ must remain unchanged. The targeted main run used the existing dev environment without syncing dependencies, with PYTHONPATH pointing at src, tests and tests/acceptance and all temporary/cache paths inside this worktree:

```sh
uv run --offline --no-sync pytest -p conftest order-out/tests/acceptance/test_observation_ingest.py -q -p no:cacheprovider --basetemp .order-run/pytest --tb=short
```

Exit 1; every collected case failed with the behaviour assertion pasted above. This is the required pre-implementation red baseline, not a passing implementation check. No full suite, xdist, mutation implementation or network call was used. The published Done when path remains tests/acceptance/test_observation_ingest.py, as requested.

```sh
fa order publish --dry-run order-out/plan.json
```

Executed via uv run --offline --no-sync using current worktree source, with a temporary sitecustomize harness injecting FakeTracker and a StateStore under the worktree. The tracker is seeded only with ../issue-1025.json (title/body); live issue labels and relationships were not available. Thus the preview's lane:main for #1025 is the offline fake's default, not a verified live lane. Network was not attempted. Exit 0:

```text
Plan (dry run): Split #1025: observation ingest first
Files: docs/specs/2026-10-31-observation-ingest-split.md, tests/acceptance/test_decision_tracking.py, tests/acceptance/test_observation_ingest.py

lane:board
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| observation-ingest | Keep GitHub observations and local writes in order | m | — | no | no |

lane:main
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| 1025 | Track decision links and derive completion from published work | m | — | no | no |
```
