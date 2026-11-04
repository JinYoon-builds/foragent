# Keep approval evidence usable across holds and retries

## Goal
Re-approving an issue records the current approval even while the issue is waiting for a person or is blocked. The hold stays in force until a person resolves it. After a process restart, a passing resolve can merge the unchanged, approved PR through the ordinary checks.

If an approval record is missing, resolve refuses before writing anything. It tells the caller that approval evidence is missing and asks them to re-approve. It preserves the approved label, the hold, the PR and the local execution state.

After an order edit changes this issue's locked acceptance files, a retry from a WIP branch that already merged an earlier main update receives the approved files without a person merging main into the issue branch. It preserves the branch's own implementation work and still rejects edits to locked files made by that branch.

The gate remains the writer of approval records. Resolve reads the evidence; it does not reconstruct or grant approval.

## Evidence and scope
The PM classified the 2026-11-02 incidents #1004, #1482 and #1527 as defects with settled required behavior. There is no product choice in this order.

On main `9ce5ac0ab52cdf649983c6342061883d0d238a06`, six new approval/resolve locks and four new WIP retry locks fail. The existing eight stale-branch locks pass but do not cover an earlier main merge in WIP history. They are insufficient to prove case B. The new retry locks cover successive order edits to the same or another locked file, with local and remote saved branches.

One issue in the orch lane owns the result. Its scheduler and orchestrator paths handle approval evidence, holds and resolution. Size m: the prototype crosses approval capture, waiting-state presentation, resolve preflight and retry restoration, while retaining the existing approval and integrity checks. No new lane, dependency, shared-state format or approval authority is needed.

## Observable acceptance
- Both needs-human and blocked issues retain their hold when re-approved. Missing or older approval evidence is refreshed durably, without launching a worker or reviewer. Repeated ticks do not duplicate approval comments. After reopening the local store, a passing resolve merges the approved, unchanged PR.
- A resolve with missing approval evidence refuses for either verdict, returns a failure, and explicitly asks for re-approval. It makes no filesystem write, tracker mutation, PR change, worker call or tick kick.
- Retries with a saved local or remote WIP branch, including one that already merged a previous main correction, receive the approved locked files. A branch's own source changes survive. A branch's own locked-file edits still invalidate approval before execution.
- Ordinary CI, Done when, approval integrity and merge checks continue to apply to resolve.

## Out of scope
Changing approval authority, automatically answering a human hold, weakening hash or merge checks, rewriting unrelated branches, new retry policies, production GitHub repair, and publishing this draft.

## Verification
The plan embeds both new candidate acceptance files. It exercises the real public tick, CLI and runner with FakeTracker, fake executors and local Git repositories. It does not lock private implementation names, approval JSON shapes or the lane's design. Existing acceptance files remain unchanged.

Each Done when command runs from the repository root, one file at a time:

```sh
uv run pytest -q tests/acceptance/test_approval_record.py
```

```sh
uv run pytest -q tests/acceptance/test_approval_record_retry.py
```

```sh
uv run pytest -q tests/acceptance/test_night_shared_locks.py
```

```sh
uv run pytest -q tests/acceptance/test_orch_merge_approved_inputs.py
```

```sh
uv run pytest -q tests/acceptance/test_orch_verbs.py
```

```sh
uv run pytest -q tests/acceptance/test_review_only_base_move.py
```

```sh
uv run pytest -q tests/acceptance/test_resume_after_question.py
```

```sh
uv run pytest -q tests/acceptance/test_stale_branch_locks.py
```

```sh
uv run pytest -q tests/acceptance/test_keep_unfinished_work.py
```

```sh
uv run pytest -q tests/acceptance/test_retry_fixes_not_redoes.py
```

```sh
uv run pytest -q tests/acceptance/test_scorecard_removed_approval.py
```

## Approval boundary
This is an unpublished draft. Candidate tests still need the repository's lane preparation and independent review before locking and publication. No human confirmation or product decision is requested by this draft.
