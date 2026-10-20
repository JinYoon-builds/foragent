# P7d spec: lanes and parallel runs, fresh-base merge, transient executor errors

Why: `docs/decisions.md` D21 (lanes), D22 (lane = agent = area; the gate-side part), D24 (transient errors). Read `src/foragent/{scheduler,runner,executors,state,config,policy,labels,inspect_data}.py`. Offline tests only.

## 1. Lanes (gate side)
- Config: `lanes` mapping, default `{}`; each entry `{paths: [glob...], agent: str | null, executor: codex|claude|null, model: str | null}`. `max_parallel` (default 2, ≥1). Validate names `[a-z0-9-]+`.
- An issue's lane = its `lane:<name>` label; no label → `main` (implicit lane, always exists, uses project defaults). A label naming a lane not in config → treat as `main` and report it once per tick in actions.
- Scheduler start step: at most **one active run per lane** and at most `max_parallel` active runs per project. Each tick, for every lane without an active run (lane order: name ascending, `main` last), start the lowest-numbered ready issue of that lane while the project cap allows. Integrity re-check before each start (existing). Store records carry `lane`.
- Effective executor/model for a run: lane `executor`/`model` if set, else project defaults.
- Lane agent (D22): if the lane has `agent: fa-<name>` and `.claude/agents/fa-<name>.md` exists in the worktree:
  - claude implementer: add `--agent fa-<name>` to the argv (everything else unchanged: /goal, settings, stream-json).
  - codex implementer: read that file's body (strip frontmatter) and, if present, `.claude/agent-memory/fa-<name>/MEMORY.md` (first 200 lines), and prepend both to the prompt under `Lane notes:`; never write memory for codex.
- `fa gate status` groups running/ready issues by lane.
- Paths of lanes are not enforced as a hard boundary in v1 (order assigns lanes; inspect will show cross-lane edits later). Record changed paths outside the lane's globs in the PR body under "Outside lane".

## 2. Fresh-base merge (parallel safety)
Before merging (fast mode into base, or batch mode into the batch branch):
- Fetch the target branch. If the target head is not an ancestor of the PR head (i.e. the target moved since the worktree was created), in the worktree: `git merge --no-edit origin/<target>`.
  - Conflict → keep the merge in progress and its conflict markers in the worktree. Within the existing attempt budget and while holding the project merge lock, ask the implementer to resolve the listed files, preserve both the new base features and the issue features, and leave commits to the runner. Reject remaining conflict markers even if the implementer staged them; otherwise the runner stages the resolution, creates the merge commit, and reruns Done when before pushing and waiting for CI on the verified SHA. Remaining markers or failed checks consume another attempt; exhaustion → retries-exceeded. Claude implementer settings deny `git merge` commands as well as commits.
  - Clean merge → re-run Done when (same independent check). Fail → failed attempt as above. Pass → push, wait for CI again on the new head, merge bound to the new verified SHA. Do not re-run the reviewer for a clean base merge; note "base merged before merge: <sha>" in the PR (comment).
- Loop at most 3 times if the target keeps moving; then flag `timeout` ("base kept moving").

## 3. Transient and limit errors (D24)
- Classify executor results (exit code, last message, the tail of the run's log section for this execution): 
  - transient: `at capacity`, `overloaded`, `529`, `503`, `Service Unavailable`, `ECONNRESET`, `connection reset`, `stream disconnected`;
  - limit: `rate limit`, `usage limit`, `429`, `quota`, `limit will reset`;
  - otherwise normal.
- transient → do not consume an attempt; wait 2, 4, 8, 16 minutes (sleep injectable) and re-run the same attempt; after `transient_max_wait_minutes` (default 30) of total waiting → flag `retries-exceeded` with summary "Executor unavailable (transient errors)". If `executor_fallback_model` (default null) is set, after the second transient error switch the model to it for the rest of the run and note it in the PR.
- limit → do not consume an attempt; mark the lane paused in the store until now + `limit_wait_minutes` (default 60); finish the run with outcome `limit-wait`, return the issue to `ready` (remove `running`); scheduler skips paused lanes until the pause expires.
- Time spent waiting is excluded from the runtime budget: track `waited_seconds` in the run record; both the runner budget and the tick overtime backstop subtract it.
- Same classification for the reviewer (transient → wait/retry; limit → treat as no usable verdict only after the wait budget).

## 4. Prepared output (P7b QA)
`fa inspect prepare` stores stdout and stderr separately; text and JSON show stdout; stderr only when exit code ≠ 0 (last 1500 chars).

## Tests
Lane assignment incl. unknown label; per-lane single run + project cap; lane executor/model override; claude `--agent` argv; codex prompt gets lane notes and memory; status grouping; outside-lane note. Fresh-base: target unchanged → no merge; moved + clean → merge, recheck, new SHA merge; moved + same-line conflict in a real Git repository → resolve in place, preserve both features, create a two-parent merge commit, recheck, and merge the verified SHA; leftover markers (including staged markers), failed checks, and exhausted shared attempts; keeps moving → timeout. Transient/limit classification table; waits not counted as attempts; fallback model switch; limit pauses lane and returns to ready; waited_seconds excluded from budget and tick backstop. Prepared stdout/stderr split.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
