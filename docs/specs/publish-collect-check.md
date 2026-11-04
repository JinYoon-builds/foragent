# Refuse acceptance files that cannot collect on the published base

## Evidence and root cause

Prepared against 3ec75d821c6e5c68658182e8769208b29cda2b95, this checkout's HEAD and latest locally available origin/main. Read-only git ls-remote origin refs/heads/main failed because github.com could not be resolved. Remote freshness is unverified; check it before later publication. This draft makes no source changes and does not publish anything.

On 2026-11-01 c7723f0c9486e9fedcbbce985394ec732ef2f58e published the merge-path-pin lock at 21:32:22 +09:00. Its module imports tests/merge_path_expected_red.py before pytest can collect tests. The helper was an unlocked future implementation deliverable rather than a published acceptance file. It reached main in 3ec75d821c6e5c68658182e8769208b29cda2b95 at 21:52:24 +09:00, 20 minutes and 2 seconds later.

validate_plan checks schema, paths, referenced acceptance files and destinations, but never imports them. _publication_checks is shared by immediate/dry-run/deferred publication and flush preflight. It currently accepts this invalid file set, and dry-run returns before the approval checks. publish later refreshes main and _publish_files writes only the validated publication artifacts; an unlocked local helper cannot travel with them. preread validates the plan and asks the implementer while that helper can still exist in the caller checkout. The missing capability is a factual collection check against the exact prospective main tree, not a stronger semantic instruction to the agent. A blacklist for imports or a one-off merge-path exception would leave the root cause intact.

Existing reusable pieces are the shared publication validation boundary, project command discovery/normalization in order.py and run_checks.py, committed-base scratch materialization in order_scratch.py, and bounded test-slot admission in capacity.py. They do not currently join into publication collection validation. Their internal functions are evidence for the lane, not new locked APIs or an implementation sequence.

## Required outcome

Every order publication is collectable in the tree main would actually receive. Before any order or lane-setup commit, fa order publish validates a clean view of the current base plus exactly the acceptance-file contents that this order would publish. Include explicit acceptance_files and acceptance files resolved from Done when; use the retained contents for deferred entries. Existing tracked base files remain available. Dirty or untracked caller files, caller-only import paths, proposed implementation files, and another deferred order's unpublished files cannot satisfy imports. Collect only this order's acceptance files using the configured project test command, or the existing detected command when none is configured, without executing test bodies or collecting unrelated tests. Reuse the existing publication validation boundary and command-resolution behavior; leave internal structure and APIs to the lane.

This protection applies without --approve, with --approve or selected approvals, with --dry-run, and when registering --defer entries. fa order preread performs the same check before invoking an implementer or recording a clean answer. fa order flush revalidates each retained order on the current base before any batch commit. A successful earlier preread or defer cannot waive a later base change. The base checked for an immediate publication must be the base actually used for its commit; a base move during publication cannot open a validation-to-write gap.

An import or other collection error refuses the operation with the failing acceptance path and the underlying exception type and useful error text, including the missing module name. For such a refusal, publication files, the caller index, main history, remote main, GitHub issues/labels/approvals, and deferred order contents stay unchanged. A rejected preread invokes no model and records no successful preread. Temporary validation files and read-only base/ref refreshes are allowed; clean them up. Diagnostic wording is the lane's choice. A locked test whose body raises AssertionError after successful collection remains publishable. Collectable helper modules and a successful collection with no test functions do not become runtime failures.

Fail closed when the collection check itself cannot establish a result: no usable base, no usable collection command, missing runner/dependency, timeout or admission failure. With origin configured, inability to refresh its configured base is unavailable; a stale local branch cannot certify current-base collection. Clearly distinguish this unavailable check from an acceptance import error, name the attempted files and command where available, and report the cause instead of reporting a pass. No fallback may use a dirty caller tree or silently skip the check. A plan with no acceptance files has no collection work.

Keep collection short: its execution and any test-slot admission wait share a deadline of at most 30 seconds per order, collect only the selected acceptance paths, and release any held test slot on success, refusal or timeout. Do not run Done when, build a baseline, launch an implementer, or run the full suite to decide collection. Existing Git transport limits stay as they are. The lane chooses slot use and process handling consistent with that finite budget and the existing process-safety rules. This is a factual test importability check, not a script deciding test meaning or an agent's repair approach.

The locked public-CLI cases cover a missing helper despite its presence in the caller checkout; the same helper included in acceptance_files; a runtime-red test; publish with and without approval; dry-run; preread; defer; and flush after a valid retained entry loses a dependency on main. Controls enforce use of the project's configured launcher and reject unrelated collection. Additional cases cover stale local main, an unavailable current remote base and an unavailable runner. Keep the named existing locked controls passing. No existing locked assertions may be weakened.

## Fail-closed decision

A missing runner, unsupported command or unavailable base supplies no evidence that main can collect. Failing open would recreate the same shared-main outage through a different error path. Refuse before durable publication and explain the unavailable check so the orderer can repair the environment. This follows the existing fail-before-write publication/preflight pattern and the project's treatment of unavailable acceptance evidence as unavailable rather than green. Runtime red is a legitimate implementation contract; collection failure is a broken contract delivery.

The 30-second collection/admission cap is a draft assumption defining a short check, not a new general test or Git timeout. Completed helper-only collection is allowed. The empty acceptance-file case is a no-op. The lane retains implementation choices, including process invocation, temporary-tree mechanics, internal names and diagnostics, within these outcomes.

## One issue and ownership

One issue, key publish-collect-check, lane order, size s, no dependencies, no statements or new lanes. .foragent.yml assigns order.py and tests/test_order.py to order; board also lists order.py, and the narrower order lane is the natural owner of order validation. The unassigned flush boundary and a possible small order-side module belong to this same fix. No separate scheduler, harness or registry issue is created.

Files:
- src/foragent/order.py
- src/foragent/order_collect.py
- src/foragent/order_flush.py
- tests/test_order.py
- tests/acceptance/test_publish_collect_check.py

The new locked test is embedded completely in acceptance_files. The standalone matching copy lives under _drafts/publish-collect-check/tests/acceptance/. Existing locked controls are only named in Done when and are unchanged.

## Scope

Changing src/ behavior beyond this collection validation; implementing or redesigning merge-path behavior; changing tests/merge_path_expected_red.py, xfail exemptions, acceptance lock boundaries, policy, harness_guard, review rules, approval semantics, flush recovery, Git transport, scheduler or global capacity policy; semantic keyword/regex classifiers, similarity cutoffs, model decisions in scripts or imposed agent step orders; changing existing locked tests; publishing this draft. The unlocked-registry safety assessment is report-only.

## Verification contract and observed base results

The lock calls cli.main for publish, preread and flush. Each case uses real local Git objects and a local bare origin, real command/configuration readers, and real publication and preread bindings. Only Git commit/push transport is emulated with fast-import/fetch, GitHub writes use FakeTracker, and the model answers NONE. The repository's forbidden-command and HOME/signal guards remain enabled. No collection validator or result is replaced. Missing-module diagnostics must name the acceptance file, ModuleNotFoundError and the missing helper. No complete diagnostic sentence or private collector API is pinned.

The intended post-implementation result is that every new case passes. On unchanged source the final draft run had 10 expected failures and 2 passing positive controls in 6.90 seconds, with no collection or fixture errors. Invalid immediate, approved, deferred, dry-run, preread and flush operations are currently accepted; an unavailable runner or current remote base is also ignored. The helper-included and runtime-red publication controls already pass. Those positive controls prevent a blanket publication ban.

Before those final new-lock results, the exact existing locked Done-when selection passed all 15 cases in 10.87 seconds. An earlier broader focused selection had 18 passes and 2 pre-existing failures, both interruption-recovery variants in test_d85_batched_publication.py. Recovery is outside this order; the passing normal-boundary and clean-preread cases are the selected controls. Do not claim the interrupted recovery tests passed and do not weaken them.

Done when:
- uv run pytest -q tests/acceptance/test_publish_collect_check.py
- uv run pytest -q tests/acceptance/test_order_preread.py tests/acceptance/test_publish_own_paths.py tests/acceptance/test_d85_batched_publication.py::test_boundary_publishes_orders_and_notes_together_without_creating_approval tests/acceptance/test_d85_batched_publication.py::test_waiting_never_bypasses_the_existing_clean_preread_requirement

Never run the full repository suite for this order. DIAGNOSIS.md retains invocation details, the incident evidence, actual test results and the requested report-only registry assessment. No implementation or post-fix green run is claimed.
