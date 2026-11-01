# Resume stale issue branches with approved acceptance locks

## Outcome
A republished and re-approved order can resume without another approval loop when the configured base already carries its approved locked acceptance files.

## Required behavior
- An existing issue branch with no own commits and older inherited locked bytes reaches implementation with the approved bytes on the current base. This applies to locally saved and remote-only branches, including an existing stale batch target.
- When own commits affect only implementation files, the run adopts the approved base and keeps that implementation work.
- If an issue's own commit edits a locked acceptance file, startup still invalidates the approval before the implementer runs. Refreshing the base must not erase or excuse that edit.
- The existing rejection when the configured base differs from the approved snapshot remains in force.

## Scope
One issue, size s, orch lane. The affected behavior is run startup and reuse of existing issue branches. Internal structure, names, and the synchronization mechanism belong to the lane. No new agent judgment rules or agent procedures are needed.

## Out of scope
Changes to role-signal behavior, acceptance contents, approval authority, publishing, retry budgets, or existing operational branches. No weakened acceptance validation.

## Acceptance
The locked test drives the public run entry with a fake tracker and executor and disposable local Git histories. It checks both local-only and remote-only saved branches, stale batch startup, approved bytes at executor entry, preserved implementation work, and invalidation for each own locked-file edit. A fixture-only executor question ends a successfully admitted run after that boundary is observed.

```sh
uv run pytest -q tests/acceptance/test_stale_branch_locks.py
```
