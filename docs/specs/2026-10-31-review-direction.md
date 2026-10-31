# Review in one wide round, then confirm

## Authority and QA decisions
Jin selected this direction on 2026-10-31. The orchestrator, acting as test owner, now authorizes corrected copies of the conflicting locked review tests in this order. Original src/ and tests/ are unchanged during preparation; the replacements are carried as acceptance_files, ready for the publishing path to apply later. No implementation or actual publication is requested.

QA item 1 is a planning judgment problem: the prior plan bundled independently verifiable outcomes into two size-l issues. The agent lacked a sharp separation of observable outcomes. This revision supplies seven separate Goals, locked files and only genuine dependencies. Every issue is s or m; none adds a semantic rule to a script.

QA item 2 is an authority/context problem in planning judgment, not a tool bug: the earlier agent left superseded count expectations as unresolved blockers because the locked-test owner had not authorized replacements. The owner's current instruction supplies that permission and the new expected behavior. This order corrects those expectations in copied acceptance files. Defect classification and deciding whether new ground is a real defect remain the reviewing agent's judgment; the lane receives context and instructions, not keyword or similarity rules in production.

The specific instruction to leave the four pre-existing failures in test_worker_launch_budget.py untouched takes precedence over the general instruction to correct all fourteen quoted conflicts. Thirteen of that original inventory are corrected in three copied files. Its four existing failing cases, including the fourteenth quoted fan-out count conflict, remain byte-for-byte unchanged and are excluded from Done when. The project-override expectation also changes as a necessary consequence of replacing the seven defaults with four; this is one additional affected test, not a new product requirement.

## Evidence and root cause
The supplied 2026-10-30 evidence reports 136 returns, 263 findings, 149 real defects, 109 repeated findings, and 73 real defects first found in round 2 or later. These supplied observations motivate the order; no new statistical classification is claimed.

Current review coverage is partitioned by the requested reviewer count, not one reviewer for each perspective. Every round repeats the same review task without first-round class evidence as a distinct confirmation task. The merger already judges raw claims, but is not asked to collect them by defect class. Existing capacity tools are available, while review executor calls do not acquire shared workload admission. Rework already asks for a class sweep but omits the searched list in the PR. Structural NOTE records survive for the orchestrator but do not reach the next architect's input. The missing agent context is the assigned perspective, complete class evidence, later-round scope, repair-report request and proposal delivery.

## Shared observable behavior
- Today the wide first round is gated by touching a risk-map path. Every perspective reads all changed files, including unmapped files in that change. Entirely unmapped changes retain their ordinary single review.
- Four core perspectives replace the seven generic defaults: goal fidelity; concurrency and ordering; interruption and rollback; test quality. Configured project perspectives and their focus remain. Each perspective has one independent reviewer. The fixture with one additional project perspective therefore has five reviewers.
- Requested reviewer counts and per-issue overrides limit simultaneous reviewers; they never reduce required perspective coverage. Shared machine capacity may lower concurrency further. Cover every perspective in sequential batches when capacity is short.
- Collect every raw claim to completion and ask the merging reviewer to group real defects by class, retaining their evidence and affected instances. Return that authoritative result once. A union of raw verdicts or scripted wording similarity does not replace the agent's judgment.
- From round 2 onward, exactly one reviewer receives every first-round class and its evidence. It only confirms that those classes are fixed throughout the changed code, including uncited instances. No later panel or findings-merger call is needed.
- A later reviewer may return work on new ground only for a real defect. A new structural preference is a nonblocking proposal. Give the agent this scope; do not infer meaning from its prose in a script.
- Fresh and resumed lane rework prompts ask for the complete defect class to be fixed across the changed code, and a list in the PR of what was searched, where the lane looked and what it found. The agent chooses its tools and search order.
- Structural proposals outside the Goal do not block merge or cause lane rework. Keep the proposal durably available to the next architect session with issue context, either inline or through an explicit readable artifact pointer. A real defect beside a proposal remains a blocker. Communication to Jin continues through the orchestrator.
- Preserve unchanged scope rules, incident context, model strength, configuration validation, project isolation, override persistence, reviewer failure handling, locked-test conflict handling, authoritative merger judgment and non-risk/native review behavior.

## Issue boundaries and dependencies
All issues belong to orch: the configured runner and orch role sessions own these behavior changes; extracted review/prompt code follows that ownership. No new lane or third-party skill is needed. Workers select approved skills per issue; no fixed skill change is requested.

| Key | Size | Blocked by | Independent acceptance evidence |
| --- | --- | --- | --- |
| perspective-fanout | m | none | one whole-change reviewer per core/project perspective; corrected existing count/default expectations |
| merged-return | m | perspective-fanout | all raw evidence reaches one agent judgment grouped by class and one final return |
| budget-batches | m | perspective-fanout | all perspectives complete at one/two available slots with shared live accounting |
| confirm-only | m | merged-return | one later reviewer receives every first-round class and a confirmation-only task |
| new-ground | s | confirm-only | actual later-review instructions allow returns on new ground only for real defects |
| rework-prompt | s | none | fresh/resumed lane input requests whole-class fixes and the searched list in the PR |
| architect-proposals | m | none | structure-only work merges; the next architect receives the durable proposal |

Only these behavior contracts cross issues. The class merger needs complete perspective evidence. Confirmation needs the complete first-round class result. New-ground scope needs the confirming round. Batching needs full perspective coverage but not the class merger. Rework prompts and proposal delivery reuse existing feedback/NOTE paths and need no dependency on the new fan-out. File overlap in one lane is not a dependency. No private names, signatures or storage schema are prescribed.

## Corrected locked files and owning issue
All three replacements belong to perspective-fanout, which changes default coverage and the meaning of reviewer counts. Each file is supplied in full under its existing tests/acceptance path. Every existing test function name and parameter decorator/id is retained. Other assertions about safety, real outcomes, raw claims and scope remain. Test-only fake transports are sized/repeated for the new expected number of calls, return the existing verdict format for prompt-based panels, and observe batches instead of requiring all perspectives to overlap at once; these changes prevent fake exhaustion from masquerading as a feature failure. No production behavior is faked.

### tests/acceptance/test_review_perspectives.py

- `test_review_perspectives_defaults_have_exact_order_and_required_focus`
- `test_review_perspectives_project_appends_and_overrides_in_place`
- `test_review_perspectives_single_risk_reviewer_receives_effective_checklist`
- `test_review_perspectives_non_risk_prompt_omits_checklist_while_risk_prompt_has_it`

### tests/acceptance/test_review_parallel.py

- `test_review_parallel_round_robin_reviews_overlap_and_merge_alone_decides`
- `test_review_parallel_deduped_merge_findings_reach_blocked_comment_once`
- `test_review_parallel_failed_or_timed_out_reviewer_cannot_silently_pass`
- `test_review_parallel_non_risk_stays_single_and_risk_count_is_capped`
- `test_review_parallel_cli_issue_override_persists_wins_and_clear_restores_config`

### tests/acceptance/test_risk_map.py

- `test_reviewer_prompt_contains_only_risks_matching_changed_paths`
- `test_native_claude_review_receives_only_matching_risk_context`
- `test_risk_context_is_additive_and_keeps_full_review_of_unmapped_changes`
- `test_matched_review_uses_l_tier_regardless_of_issue_size`
- `test_fixed_risks_keep_incident_context_and_strength_in_review`

The default-order test changes through the expected NAMES/THEMES constants; its function body and parameter identity remain unchanged. Its project-override companion now overrides a member of the new core set, preserving the original test's override-in-place meaning. The failed/timed-out reviewer test still rejects silently passing incomplete coverage. The native-risk tests still require incident context and full-change review; a nonempty whole-change prompt or the existing native PR binding is accepted at the transport boundary.

## Disposition of the original fourteen quoted conflicts
The following are the old expectations and their originally measured conflicts, retained as evidence rather than current instructions. The corrected copies above replace thirteen; the worker-budget entry remains untouched by the owner's specific exception.

- `tests/acceptance/test_review_perspectives.py::test_review_perspectives_defaults_have_exact_order_and_required_focus`: `assert [item["name"] for item in defaults] == list(NAMES)`. The old seven default perspectives differ from the four core perspectives now selected by Jin. Preserve unrelated configuration validation and project isolation; Corrected in the copied default expectations.

- `tests/acceptance/test_review_perspectives.py::test_review_perspectives_single_risk_reviewer_receives_effective_checklist`: `assert len(calls) == 1, "N=1 must make exactly one reviewer call and no merge call"`. A concurrency setting of one now queues every perspective; it no longer collapses the first round into one generalist.

- `tests/acceptance/test_review_perspectives.py::test_review_perspectives_non_risk_prompt_omits_checklist_while_risk_prompt_has_it`: `assert len(matched.reviewer.calls) == 1`. The positive risk control requires a single call. Its non-risk control remains valid.

- `tests/acceptance/test_review_parallel.py::test_review_parallel_round_robin_reviews_overlap_and_merge_alone_decides`: `expected = [CHECKLIST[index::3] for index in range(3)]`. One reviewer per perspective replaces round-robin shares. The authoritative merge judgment and full raw evidence remain valid.

- `tests/acceptance/test_review_parallel.py::test_review_parallel_deduped_merge_findings_reach_blocked_comment_once`: `assert_panel(panel, 2)`. The fixed two-reviewer panel conflicts with perspective-count coverage. Keep the deduplicated final return behavior.

- `tests/acceptance/test_review_parallel.py::test_review_parallel_failed_or_timed_out_reviewer_cannot_silently_pass`: `assert len(panel.reviews) == 3, "The failing reviewer must actually be invoked"`. The fixed three-reviewer count conflicts. Failed or incomplete review coverage must still never silently pass.

- `tests/acceptance/test_review_parallel.py::test_review_parallel_non_risk_stays_single_and_risk_count_is_capped`: `assert_panel(capped, len(CHECKLIST))`. Its seven-default panel cap differs from the four new core perspectives plus configured project perspectives. Preserve non-risk single review and one perspective per reviewer.

- `tests/acceptance/test_review_parallel.py::test_review_parallel_cli_issue_override_persists_wins_and_clear_restores_config`: `observe(1, 3)`. This checks total reviewer count rather than simultaneous reviewers. Preserve override persistence, issue isolation, invalid-input rejection and clearing semantics for the concurrency limit.

- `tests/acceptance/test_worker_launch_budget.py::test_reviewer_fanout_counts_each_simultaneous_session_and_merges_once`: `assert maximum[0] == 2 and len(reviews) == 3 and len(merges) == 1`. The total of three reviewers conflicts; the capacity maximum of two and one final merge remain valid.

- `tests/acceptance/test_risk_map.py::test_reviewer_prompt_contains_only_risks_matching_changed_paths`: `assert len(case.reviewer.calls) == 1`. The mapped first-round single-call assertion conflicts. Keep matching-only incident context, full-change scope, native reviewer support and risk-tier strength.

- `tests/acceptance/test_risk_map.py::test_native_claude_review_receives_only_matching_risk_context`: `assert run_issue(1, case.ctx) == "merged" and len(case.review_contexts) == 1`. The mapped first-round single-call assertion conflicts. Keep matching-only incident context, full-change scope, native reviewer support and risk-tier strength.

- `tests/acceptance/test_risk_map.py::test_risk_context_is_additive_and_keeps_full_review_of_unmapped_changes`: `assert run_issue(1, case.ctx) == "merged" and len(case.review_contexts) == 1`. The mapped first-round single-call assertion conflicts. Keep matching-only incident context, full-change scope, native reviewer support and risk-tier strength.

- `tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size`: `assert len(case.reviewer.calls) == 1`. The mapped first-round single-call assertion conflicts. Keep matching-only incident context, full-change scope, native reviewer support and risk-tier strength.

- `tests/acceptance/test_risk_map.py::test_fixed_risks_keep_incident_context_and_strength_in_review`: `assert run_issue(1, case.ctx) == "merged" and len(case.reviewer.calls) == 1`. The mapped first-round single-call assertion conflicts. Keep matching-only incident context, full-change scope, native reviewer support and risk-tier strength.

## Acceptance and break-it
Each issue has its own new locked file. The new checks use public runner/executor or architect-session boundaries, with offline fake reviewers. They do not import a new internal implementation helper or require a storage schema.
- Fan-out rejects a generalist, perspective shares, omitted configured perspectives and path-only review.
- Merged return rejects grouping only identical text, missing raw evidence, early/duplicate return and a union of raw verdicts. The fake merging agent provides judgment; the actual prompt must request class grouping.
- Batching rejects dropped perspectives, capacity overflow, local-only accounting and leaked slots; one/two slots exercise sequential and batched coverage.
- Confirmation rejects another panel/merger, missing first-round classes and confirming only a cited line.
- New-ground cases reject structural taste as a later blocker and blanket approval that hides a new real bug. Both inspect the actual agent instructions.
- Rework rejects missing whole-class repair scope or a missing explicit searched list in both fresh and resumed lane prompts.
- Architect delivery rejects a structure-only hold, lost proposal, an orchestrator-only journal and dropping a real defect alongside a note.

## Assumptions
- This QA request supersedes the initial two-to-three-issue limit: the natural split is seven s/m issues.
- Four core perspectives replace seven generic defaults; configured project perspectives remain. Existing public default-list tests now expect the new named perspectives, without prescribing implementation structure in Goals.
- Reviewer counts and per-issue overrides cap concurrency; coverage follows the perspective count. A separate final merger remains the existing judgment path; later confirmation has one reviewer and no merger.
- All four existing worker-budget failures are an explicit exception to the general locked-test correction request. That file is not included as an edited acceptance file or changed to pass.
- Verification uses an offline archive of main at {base}, with production source unmodified and the new/edited acceptance files overlaid only in a disposable directory inside this working directory. This avoids prohibited branch operations and import collisions between original and edited files with the same names.
- Only order-out/plan.json and order-out/tests/acceptance/*.py remain as final output. No state-progress writes, live agents, network requests, source implementation, existing test edit, commit, push or actual publication occur.

## Executed QA verification on main 8d8d3e9fc527642b1bf21fcc4faa1568a62a0ca1
A read-only `git archive` of this exact main commit was extracted under order-out/.verify/main, and only the new/edited acceptance copies were overlaid there. Production source was not changed. The actual interpreter was /Users/apple/Dev/foragent/.venv/bin/python (pytest 9.1.1). Each file below ran separately from that main mirror with:
```
python -B -m pytest -q --tb=short -p no:cacheprovider --basetemp=<inside order-out/.verify> tests/acceptance/<file>
```
PYTHONPATH selected the mirror's src, tests and tests/acceptance; bytecode and plugin autoload were disabled. HOME, TMPDIR and all pytest temporary paths were inside this working directory. There was no xdist or whole-suite run. All cases collected; no collection, fixture-exhaustion or transport-format failure was accepted as red evidence.

| File | Status | Actual final result |
| --- | --- | --- |
| test_review_perspective_fanout.py | new | 1 failed in 4.31s |
| test_review_merged_return.py | new | 2 failed in 4.12s |
| test_review_budget_batches.py | new | 2 failed in 4.58s |
| test_review_confirm_only.py | new | 1 failed in 3.33s |
| test_review_new_ground.py | new | 2 failed in 5.47s |
| test_review_rework_prompt.py | new | 2 failed in 4.90s |
| test_review_structure_proposals.py | new | 2 failed in 3.36s |
| test_review_perspectives.py | owner-corrected copy | 4 failed, 1 passed in 5.79s |
| test_review_parallel.py | owner-corrected copy | 5 failed in 10.14s |
| test_risk_map.py | owner-corrected copy | 15 failed, 18 passed in 42.53s |
| test_review_what_how.py | unchanged | 5 passed in 8.87s |
| test_review_spec_conflict.py | unchanged | 3 passed in 5.38s |
| test_orch_review_handoff.py | unchanged | 3 passed in 7.47s |
| test_skills_review_scope.py | unchanged | 3 passed in 6.56s |
| test_retry_fixes_not_redoes.py | unchanged | 21 passed in 50.51s |
| test_orchestrator_scorecard_skill.py | unchanged | 3 passed in 3.89s |
| test_agents_see_whole_picture.py | unchanged | 7 passed in 7.98s |
| test_facts_repeated_work.py | unchanged | 8 passed in 1.00s |
| test_orch_verbs.py | unchanged | 7 passed in 6.01s |
| test_skills_per_issue.py | unchanged | 4 passed in 7.03s |
| test_worker_launch_budget.py | unchanged explicit exception | 4 failed in 4.49s |

The seven new files have 12 missing-feature failures. The three owner-corrected files have 24 missing-feature failures and 19 unchanged cases passing. The ten other review-lock files have all 64 cases passing. The specific worker-budget exception retains the same four pre-existing failures; its complete file was verified byte-for-byte against the archived main blob and was not added to acceptance_files.

Finalized fixture checks: the risk fake uses the real executor's public native-review option to select its wire format; this is a transport adaptation, not a prose classifier. Every fake class-merge response has raw evidence for both retained classes, so the merging fake never manufactures the second class. Those finalized files were rerun before recording these results.

### Actual missing-feature failure lines

`test_review_perspective_fanout.py`:
```text
E   AssertionError: Round 1 needs 5 perspective reviewers; got 1
```
```text
FAILED tests/acceptance/test_review_perspective_fanout.py::test_risk_round_assigns_each_perspective_the_whole_change
```

`test_review_merged_return.py`:
```text
E   AssertionError: Merge findings by defect class, not only identical text
```
```text
FAILED tests/acceptance/test_review_merged_return.py::test_all_raw_evidence_reaches_one_defect_class_merge_and_one_return[real-classes-return-once]
FAILED tests/acceptance/test_review_merged_return.py::test_all_raw_evidence_reaches_one_defect_class_merge_and_one_return[dismissed-claims-pass]
```

`test_review_budget_batches.py`:
```text
E   AssertionError: Return exactly one authoritative merged result
E   AssertionError: ['Shared machine accounting: active=2, holders=1, budget=2', 'Shared machine accounting: active=3, holders=1, budget=2', 'Shared machine accounting: active=4, holders=1, budget=2', 'Shared machine accounting: active=5, holders=1, budget=2']
```
```text
FAILED tests/acceptance/test_review_budget_batches.py::test_short_machine_budget_batches_all_perspectives_without_dropping_any[sequential]
FAILED tests/acceptance/test_review_budget_batches.py::test_short_machine_budget_batches_all_perspectives_without_dropping_any[two-at-a-time]
```

`test_review_confirm_only.py`:
```text
E   AssertionError: Confirmation lost first-round evidence: stale-receipt-owner
```
```text
FAILED tests/acceptance/test_review_confirm_only.py::test_round_two_has_one_confirmer_with_all_first_round_classes
```

`test_review_new_ground.py`:
```text
E   AssertionError: New ground returns work only for a real defect
```
```text
FAILED tests/acceptance/test_review_new_ground.py::test_confirmation_new_ground_only_returns_work_for_a_real_defect[new-structure-note]
FAILED tests/acceptance/test_review_new_ground.py::test_confirmation_new_ground_only_returns_work_for_a_real_defect[new-real-bug]
```

`test_review_rework_prompt.py`:
```text
E   AssertionError: The lane must be asked to put what it searched in the PR
```
```text
FAILED tests/acceptance/test_review_rework_prompt.py::test_rework_prompt_requires_class_wide_repairs_and_a_searched_list_in_the_pr[fresh-lane]
FAILED tests/acceptance/test_review_rework_prompt.py::test_rework_prompt_requires_class_wide_repairs_and_a_searched_list_in_the_pr[resumed-lane]
```

`test_review_structure_proposals.py`:
```text
E   AssertionError: Recorded structure proposal never reached the architect's input
```
```text
FAILED tests/acceptance/test_review_structure_proposals.py::test_structure_proposal_survives_merge_or_hold_and_reaches_the_next_architect[structure-only-passes]
FAILED tests/acceptance/test_review_structure_proposals.py::test_structure_proposal_survives_merge_or_hold_and_reaches_the_next_architect[real-defect-still-blocks]
```

`test_review_perspectives.py`:
```text
E   AssertionError: assert ['security', ..., 'docs', ...] == ['goal-fideli...test-quality']
E   AssertionError: assert ['security', ..., 'docs', ...] == ['goal-fideli...bservability']
E   AssertionError: N=1 queues all round-1 perspectives, then one merge
E   AssertionError: Risk round covers every perspective, then merges
```
```text
FAILED tests/acceptance/test_review_perspectives.py::test_review_perspectives_defaults_have_exact_order_and_required_focus
FAILED tests/acceptance/test_review_perspectives.py::test_review_perspectives_project_appends_and_overrides_in_place
FAILED tests/acceptance/test_review_perspectives.py::test_review_perspectives_single_risk_reviewer_receives_effective_checklist
FAILED tests/acceptance/test_review_perspectives.py::test_review_perspectives_non_risk_prompt_omits_checklist_while_risk_prompt_has_it
```

`test_review_parallel.py`:
```text
E   AssertionError: Expected exactly one findings merge call, got 0
E   AssertionError: Expected 4 independent reviewers, got 3
E   AssertionError: The failing reviewer must actually be invoked
E   AssertionError: Expected exactly one findings merge call, got 6
```
```text
FAILED tests/acceptance/test_review_parallel.py::test_review_parallel_round_robin_reviews_overlap_and_merge_alone_decides
FAILED tests/acceptance/test_review_parallel.py::test_review_parallel_deduped_merge_findings_reach_blocked_comment_once
FAILED tests/acceptance/test_review_parallel.py::test_review_parallel_failed_or_timed_out_reviewer_cannot_silently_pass
FAILED tests/acceptance/test_review_parallel.py::test_review_parallel_non_risk_stays_single_and_risk_count_is_capped
FAILED tests/acceptance/test_review_parallel.py::test_review_parallel_cli_issue_override_persists_wins_and_clear_restores_config
```

`test_risk_map.py`:
```text
E   AssertionError: Four core perspective reviews and one merge are required
```
```text
FAILED tests/acceptance/test_risk_map.py::test_reviewer_prompt_contains_only_risks_matching_changed_paths[src/runtime/stop.py]
FAILED tests/acceptance/test_risk_map.py::test_reviewer_prompt_contains_only_risks_matching_changed_paths[src/runtime/nested/stop.py]
FAILED tests/acceptance/test_risk_map.py::test_reviewer_prompt_contains_only_risks_matching_changed_paths[scripts/stop.py]
FAILED tests/acceptance/test_risk_map.py::test_native_claude_review_receives_only_matching_risk_context[claude-local]
FAILED tests/acceptance/test_risk_map.py::test_native_claude_review_receives_only_matching_risk_context[pr-body]
FAILED tests/acceptance/test_risk_map.py::test_risk_context_is_additive_and_keeps_full_review_of_unmapped_changes[codex]
FAILED tests/acceptance/test_risk_map.py::test_risk_context_is_additive_and_keeps_full_review_of_unmapped_changes[claude-local]
FAILED tests/acceptance/test_risk_map.py::test_risk_context_is_additive_and_keeps_full_review_of_unmapped_changes[claude-pr-body]
FAILED tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size[s-codex]
FAILED tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size[s-claude]
FAILED tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size[m-codex]
FAILED tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size[m-claude]
FAILED tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size[l-codex]
FAILED tests/acceptance/test_risk_map.py::test_matched_review_uses_l_tier_regardless_of_issue_size[l-claude]
FAILED tests/acceptance/test_risk_map.py::test_fixed_risks_keep_incident_context_and_strength_in_review
```

Call-count assertions in the indexed panel fakes identify the expected review/merge boundary. For example, “got 6” after four expected perspective calls is the old oversized panel crossing that boundary, not evidence that main actually launched six merging agents. The separate class-merge test identifies the merger by an exact supplied evidence marker and fails directly on the missing class-grouping instruction.

### Explicitly unchanged pre-existing failures
```text
E   AssertionError: assert ('error' == 'merged'
E   AssertionError: Stop hook omitted the shared test slot
E   AssertionError: assert 'held' == 'merged'
FAILED tests/acceptance/test_worker_launch_budget.py::test_unadmitted_runner_waits_for_shared_capacity_before_work[issue]
FAILED tests/acceptance/test_worker_launch_budget.py::test_unadmitted_runner_waits_for_shared_capacity_before_work[review]
FAILED tests/acceptance/test_worker_launch_budget.py::test_stop_hook_check_holds_test_slot_without_double_counting_its_worker
FAILED tests/acceptance/test_worker_launch_budget.py::test_reviewer_fanout_counts_each_simultaneous_session_and_merges_once
```
### Identity audit and preparation boundary
The three edited copies retain all 24 original test functions and every parameter decorator/id. The changed default oracle is recorded through its expected constants, and its project override now targets the new core set. No original src/ or tests/ path was edited, and no prohibited Git command or live reviewer was run. The two previously bundled new test files were removed, with their behaviors covered by the seven new per-issue files. acceptance_files were regenerated from the final output bytes.

### Dry-run setup
The final `fa order publish --dry-run order-out/plan.json` uses the real CLI with the checkout's unchanged source, an isolated HOME under order-out/.verify, and an offline gh fixture. The fixture supplies only an empty repository issue listing and refuses every other gh operation. It validates schema, lanes, dependency keys and publication file planning without checking live GitHub state, approving or actually publishing anything.

## Additional unchanged review-lock inventory
The final inventory also includes these review-specific cases located in other acceptance files. Each explicit node target ran with pytest -q under the same archived main and offline environment; unrelated cases in those files were not selected.

| Exact unchanged target | Actual result |
| --- | --- |
| test_claude_only.py::test_claude_only_only_claude_chooses_claude_for_execution_and_fresh_review | 1 passed in 1.26s |
| test_conflict_budget.py::test_conflict_uses_its_own_budget_after_review_rounds | 1 passed in 3.45s |
| test_executor_choice.py::test_executor_choice_exhausted_side_hands_worker_and_reviewer_to_the_other | 1 passed in 0.63s |
| test_executor_choice.py::test_executor_choice_fixed_yaml_lane_and_configured_reviewer_win | 1 passed in 2.27s |
| test_resume_after_question.py::test_resume_after_question_each_review_retry_resumes_with_verbatim_feedback | 4 passed in 11.66s |
| test_scorecard_records.py::test_scorecard_records_each_review_round_is_journaled_before_the_next | 2 passed in 4.79s |
| test_scorecard_records.py::test_scorecard_records_spec_question_adds_no_review_event_to_an_existing_journal | 1 passed in 2.48s |
| test_short_merge_lock.py::test_merge_records_review_latency_and_lock_hold_from_fake_clock | 2 passed in 5.24s |

These additional 13 cases passed. The final unchanged-review verification is therefore 77 passing cases, plus the four explicitly exempt pre-existing worker-budget failures. New checks remain 12 missing-feature failures; corrected copies remain 24 missing-feature failures and 19 passing unchanged cases. No non-feature failure or test identity change was introduced.

The real CLI dry-run returned exit code 0 and previewed seven orch issues, all size s or m, with only the dependencies shown in the issue table. The offline issue-list fixture was used; live GitHub state and actual publication were not tested. A final dry-run is repeated after this verification text is included. No approval or publication is performed.
