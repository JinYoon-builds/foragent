# Approved merge inputs and factual revert counts

## Outcome
A person's `fa orch merge` checks the same approved acceptance inputs as an ordinary merge and honors the linked issue's approved Done when commands. The scorecard reports revert commits from the default branch instead of an unwritten event type. A failed fact read stays unknown.

## A: Person-approved merge inputs
Observed defect: `src/foragent/orch.py` `_premerge` (line 355) and `merge` (line 507) check the test command, conflicts, CI and ref freshness, but omit the linked approval's locked acceptance hashes and Done when commands. `src/foragent/run_publish_merge.py` `_final_merge_inputs` (around line 110) already checks approval.acceptance_blobs on the head and configured base; `src/foragent/run_checks.py` already verifies approved Done when commands in the ordinary path. The missing shared approval-input check lets a green PR carry changed locked tests through a person-approved merge.

Close when the existing public `fa orch merge PR --approved-by PERSON` refuses a changed or missing locked file on either the exact PR head or the current configured base, and refuses any failing approved Done when command. A Done when failure remains a refusal when that command also fails on the base. Each refusal exits nonzero, merges nothing and prints one diagnostic line identifying the changed acceptance path and head/base, or the exact failing Done when command. It must not post successful approval attribution for a refused merge.

Ordinary and person-approved merging share the approved-input verification; copying the hash loop into another independent checker does not meet this order. Keep the existing merge entry point, queue/lock coordination, fresh-head/base guards, test/conflict/CI checks, approval attribution and behavior of unlinked, batch and dream PRs. A person approval continues to authorize the merge action subject to the approved issue contract.

Locked checks exercise the CLI with the existing offline Git history, fake tracker and CLI binding. They change a still-passing locked test or remove it on head/base, and separately give the linked issue a failing approved Done when command while its configured suite and CI stay green. Tests use the repo's no-real-signals and no-forbidden-Git-command fixtures.

## B: Reverts from default-branch facts
Observed defect: `src/foragent/orch_scorecard.py` `_day` (around line 95) counts local events with kind `revert`; no production writer emits that event. An absent event is therefore displayed as a known zero without observing Git history.

Close when `fa orch scorecard` derives reverts_after_merge from revert commits reachable from the repository's configured default branch in each existing scorecard time bucket. Count a canonical Git revert, whose commit message identifies the reverted commit, once at the revert commit's time. Include reverts made outside Foragent. Commits only on another branch, even the checked-out branch, do not count. Preserve the rolling last-24-hour today window and daily-median previous window, including --days.

A readable history with one revert in the window reports 1; when that revert is outside the window it reports 0. An unreadable history reports JSON null for the affected metric and visibly says unknown in human output. It must not classify an unknown value as a regression or treat it as proof of no reverts. Keep the command useful for the remaining metrics when the revert read fails. Derive this during observation with no new journal, database, cache, service, background task or network dependency.

## Scope and contracts
Both issues belong to the existing orch lane in .foragent.yml. They can be built and verified independently and have no blocked_by dependency. Use existing public CLI behavior and metric keys; private helper names and organization remain the implementer's choice. The approved-input sharing requirement above is an explicit scope constraint. Existing locked acceptance files remain byte-for-byte unchanged; both additions have new node IDs, so no replacements or node-ID migrations are needed.

Draft assumptions: the configured base_branch names the default branch; canonical revert messages provide the Git fact; commit time means committer time; JSON null represents unknown, consistent with the scorecard's other unknown metrics. Exact diagnostic prose is free as long as the line contains the reason and evidence required above.

## Verification
A: uv run pytest -q tests/acceptance/test_orch_merge_approved_inputs.py
A regression: uv run pytest -q tests/acceptance/test_orch_merge.py
B: uv run pytest -q tests/acceptance/test_orch_scorecard_revert_facts.py
B regression: uv run pytest -q tests/acceptance/test_orch_scorecard.py

Before publication, existing regression commands must pass on the unchanged source. The new behavior tests must fail on that source for the described defects, then pass after implementation. Collect exactly these four files on a clean HEAD snapshot with only this plan's acceptance_files applied. Run targeted checks only.
