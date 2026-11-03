# S2 — batch review deletion (option A)

Only fast and careful remain valid review values, including review_paths. Removed batch values fail during configuration loading and settings writes with errors naming the valid choices. Use fast as the replacement product review default; checks: local remains the product check default.

Delete the batch-branch lifecycle: no generated run targets foragent/batch-<parent>, no batch PRs, no deferred batch finalization/reconciliation, no dead facade exports or batch-only tests. A parent relationship and an old remote batch ref do not change the configured base destination. CI mode is on exactly when checks: ci, regardless of target; ordinary CI evidence/fallback validity still applies. The public run tests cover main and delivery with and without an explicit local fallback, require real recorded artifacts and CI comparison records, and include one passing local-mode control.

Merge/lane pauses and merge groups already serve their independent purposes. Preserve D85 batched order publication, reviewer budget batches, artifact/observation batching and generic Git attribute batching. Preserve ordinary parent completion and closed-issue observation that currently share a batch reconciliation routine.

Locked acceptance replacements are supplied in full in acceptance_files and mirrored under this draft. These are mixed files, so none should be deleted wholesale. An empty acceptance_files value overwrites a file; it does not delete it. No new judgment rules, keywords or scripted agent decisions are part of this order.

Assumption: fast replaces the retired batch default; careful remains the way to require human review, and merge/lane pauses remain available. Lanes follow the owner of each issue's principal source files; ancillary fixtures and documentation travel with that change. No live GitHub publication is authorized by this draft.

Config loading, settings commands/window/messages and their current guidance form one medium order issue, review-inputs. Its source edits update the existing defaults, enums and messages together; its fixture and guidance edits follow that same input contract. Keeping the attached settings replacements with these sources makes the issue independently verifiable. The runtime interface deletion is one medium orch issue: splitting its run/review/publication arguments and facade imports across independently delivered issues would leave broken callers. Test-contract retirement is a separate medium main issue whose completion checks need only the earlier review-inputs change; the new base-run acceptance and watchdog fixture migration belong to the later atomic runtime issue. The Git orphan helper is a separate small skills issue. Each issue body contains its own source/test inventory and names neighboring owners in Out of scope.

Rebased onto main `5bad4eb2cbb89721579d7fe776dcf78c303635b7` from draft base `aa7c159cbf7e7c7178eb7c1c1a3eda078edeed5b`. Every full-file replacement starts from that main and carries only the old S2 delta. The updated PR-body retention, size and structured-content checks stay intact. Keep all surviving test function names and parametrized node identities, including the historical batch-named first-use settings test; change its assertions only. PatchReplayError and its facade lock retire with its sole batch replay raise site. The unit replay-recovery harness-hold scenario is batch-only and retires too; ordinary harness holds remain. No acceptance payload for the six overlapping approved orders is supplied or replaced. See REBASE.md for file-level lock and source-scope overlap facts before publication.

Current-main source drift: include src/foragent/run_pr_body.py in base-run-lifecycle because merged #1372 added a batch-body formatter and dispatch. Preserve all ordinary structured-body and retention behavior from that merge.
The new current-main test_run_pr_body.py caller migrates atomically with the removed batch formatter. Its historical batch-named test keeps its identity and checks that an unassociated retired head supplies no aggregate body.
Keep the existing batch fixture helpers through retire-batch-tests: the newly merged test_run_pr_body.py still calls them. The lifecycle issue migrates that caller and removes those final helper definitions together, avoiding a broken intermediate issue.

### Current approved-order overlap facts

The specified issues were approved when first inspected. #1608 has since merged and closed on final main; the other five were open when inspected. Their 25 distinct linked acceptance files are already on the pinned main; none is in this order's acceptance_files, and none is overwritten. Declared implementation/test scopes overlap as follows:

- #1597: `src/foragent/runner.py`.
- #1599: `src/foragent/config.py`, `src/foragent/config_validation.py`, `src/foragent/run_conflicts.py`, `src/foragent/run_review.py`, `tests/test_config.py`.
- #1570: `src/foragent/run_implementation.py`, `src/foragent/run_review.py`.
- #1608: no shared declared implementation/test files.
- #1526: `src/foragent/ci_mode.py`, `src/foragent/orch.py`, `src/foragent/run_checks.py`, `src/foragent/run_publish_flow.py`, `src/foragent/run_publish_merge.py`, `src/foragent/run_publish_reviewed.py`, `tests/test_ci_mode_unit.py`, `tests/test_orch_premerge.py`, `tests/test_run_merge_turn.py`.
- #1527: no shared declared implementation/test files.

These shared source scopes need coordination when implementation is eventually published. This draft does not apply another order's implementation or authorize publication.

Verification-time main advance: 5bad4eb2cbb89721579d7fe776dcf78c303635b7 adds the merged #1608 preread change and order records after the first verification snapshot efb2d1d1f1038acf80ae55243663d375dde4b751. All 18 acceptance paths and all 60 prototype-change paths have identical blobs on both snapshots. The final payloads and prototype are applied and checked on the newer main.
Issue #1608 is now merged and closed on the final main snapshot. Its locked files remain unchanged and outside S2 acceptance payloads; the final proof reruns its preread acceptance alongside the rebuilt locks.
