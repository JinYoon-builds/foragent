# Publish role session memory through the existing Git flow

## Goal

After a role session changes its project memory, the next ordinary gate cycle saves the changed and newly created files to durable Git history and publishes them through the existing flow. Checkout cleanup after that boundary retains the memory, and a fresh clone reads the published bytes. Temporary delivery failure retains the local commit and an ordinary later cycle retries. Existing lane memory publication keeps working.

## Current main and remaining gap

Rebased on `43a39c7b1b6edcfdaee18f0a87b606a720b889a8`, the supplied HEAD and origin/main. This main includes #1662 (`14c5e10099cee9d340f4cafb6ec1fbe2285e06b3`).

#1662 already supplies the repository-wide main-write exclusion shared by merges, order/note publication and recovery, setup, lane settings, and the existing role-settings memory commit path in `roles_commands.py`. It holds across the final-check-to-merge interval and resolves the common Git directory across linked checkouts. This order reuses that facility; it adds no second lock and does not redo that work.

Role settings commands already force-stage their owned memory directory and commit only the owned paths. #1662 protected that existing operation; it did not introduce it. Those commands require a role-settings action, commit locally, and do not automatically collect or deliver a role session's later memory edits. Role prompts still say to leave memory uncommitted, `run_session` records completion without persistence, and the ordinary gate has no role-memory publisher. The remaining defect is that missing automatic persistence owner. The role lacks a publication facility, not content judgment or an extra Git instruction.

## Observable behaviour

- An ordinary cycle carries existing and new memory files, including supporting files hidden by an ignore rule, without a person invoking a role-settings command.
- Resolved role configuration determines the directory. The three standing roles and a custom role with a different directory receive the same persistence behaviour.
- Cleanup after the publication opportunity preserves both changed and new bytes in the checkout and in a fresh clone. If transport fails, cleanup still preserves the locally committed bytes; a later gate context retries delivery from durable state.
- Unrelated staged work stays outside the memory publication. An unchanged later cycle creates no duplicate publication.
- The new main writer participates in #1662's exclusion. A contender arriving after a successful final merge check waits or permits a safe moved-base stop; successful merges and memory publication retain both results.
- Lane pending lessons, dream curation/publication, and dream merge/redo behaviour keep working.

## Scope

One choice-free issue, lane `roles`, size `s`, no new lane. The implementation scope is role persistence under `src/foragent/orch_role*.py` and the existing scheduler connection as needed. Reuse the current Git adapter and main-write exclusion. There is no measured reason to raise the size.

Roles continue to select and write their own lessons. Persistence is mechanical plumbing. No content classifier, keyword rule, similarity threshold, script-side LLM decision, or imposed agent procedure belongs in this fix.

The lane chooses the implementation and the existing permitted commit or PR delivery route. A PR-based route must include completion under existing authority; opening an unattended PR is insufficient. Lane dreams currently publish a leased PR branch, and the orchestrator merges a memory-only PR after CI under its existing documented authority. This order changes neither that ownership nor ordinary merge approval.

## Out of scope

Memory content or curation policy; changing role/lane ownership; reimplementing #1662's lock; a new engine, scheduler or notification; new merge authority; repairing historical shared-checkout state; publishing or approving this draft.

## Acceptance boundary

“After a role session” means the next existing automated publication opportunity. Cleanup is exercised after that cycle, including after a failed delivery with a durable local commit. Protection against manual cleanup before that opportunity or machine loss before any durable write is not claimed.

Locks use public role-session, gate, runner, order and role-settings entry points. Git objects, staging, cleanup and cloning are real in disposable repositories; the injectable Git transport substitutes fast-import for commit and a local bare fetch for push. The added collector contender extends the current main serialization file without deleting or renaming its nine existing node IDs. The six original role-memory node IDs are retained, with stronger checkout and restart/retry assertions. Other acceptance files are embedded byte-for-byte from current main. Private helper names, lock paths, branch names and retry-record formats are not acceptance contracts.

## Done when

Run each command separately from the repository root; each invocation targets one file.

```sh
uv run pytest -q tests/acceptance/test_role_memory_publication.py
```

```sh
uv run pytest -q tests/acceptance/test_lane_memory_writers.py::test_dream_publishes_curated_and_consumed_pending_lines_together_or_keeps_base tests/acceptance/test_lane_memory_writers.py::test_worker_branch_forked_before_consumption_cannot_resurrect_consumed_lesson_after_union_merge
```

```sh
uv run pytest -q tests/test_orch_dream_unit.py::test_new_memory_file_in_an_ignored_directory_is_published_and_counted tests/test_orch_dream_unit.py::test_publish_with_an_open_dream_pr_pushes_nothing_and_ends_the_dream tests/test_orch_dream_unit.py::test_publish_force_pushes_leased_on_the_remote_head_it_read tests/test_orch_dream_unit.py::test_a_published_dream_completes_only_when_its_pr_merged
```

```sh
uv run pytest -q tests/acceptance/test_fa_dream_redo.py::test_redo_closes_only_requested_dream_and_starts_once
```

```sh
uv run pytest -q tests/acceptance/test_main_write_serialization.py
```

`plan.json` contains the complete acceptance files. `prototype.patch` is feasibility evidence against this main alone. `REPORT.md` records clean-main failures, prototype passes, existing-lock controls, the excluded unrelated Git-unit probe, assumptions and limits. The final draft changes no source files.
