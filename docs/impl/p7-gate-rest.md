# P7 spec: adversarial review step, batch/careful modes, stall restart

Builds on P1–P6 (read `src/foragent/*.py`). Product rules: `docs/gate.md` sections 4–6, `docs/contract.md` 추가 8-5 (batch = parent Epic, batch branch), `docs/decisions.md` D10. Offline tests (fake executors/tracker, temp git repos). Prefer extending existing modules over new abstractions.

## 1. Adversarial review step (runner, after Done when passes, before commit/PR)
- Reviewer = `config.resolved_reviewer` executor with `reviewer_model`. Runs in the same worktree with a **read-only** instruction, given only: the snapshot Goal, Out of scope, Done when commands, and `git diff --cached origin/<base>` (truncate to 60k chars). Never the implementer's reasoning or logs.
- Prompt (fixed, short): "Review this change for defects that break the stated goal, violate Out of scope, or would make the Done when commands pass without the goal being met (weakened checks, special-casing test inputs). Ignore style and nice-to-haves. Do not edit files. Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` followed by at most 5 findings, each one line with file:line and the concrete failure."
- Parse the verdict from the complete response, before any output-tail truncation. Claude retains full stdout in a file and exposes `full_message` separately from `last_message`.
- Review is fail-closed: a nonzero/missing exit code, timeout, or missing exact `VERDICT: PASS` / `VERDICT: FAIL` line is unusable. Retry the reviewer once without rerunning implementation. Unless that retry is a successful, explicit PASS, open the PR but hold merging with `risk-high`, summary "Reviewer did not return a usable verdict". Record both review rounds.
- Compare HEAD SHA, index and working tree before/after review. Restore all three if changed, preserving the implementation and any pre-existing unstaged contents, and record it.
- FAIL → counts as a failed attempt with failure text = findings; goes back to the implementer (existing retry loop, shared `max_retries`). After the implementer's next attempt, Done when runs again, then review again.
- Config `review_max: 2` (new key, default 2): maximum number of review FAILs per run; when exceeded, proceed to PR but add `risk:high`-equivalent hold: flag `risk-high` with summary "Reviewer still found defects after N rounds" and the findings, PR link in details.
- PR body gets a "Review" section: verdict per round and findings.
- Config `reviewer: none` disables the step (add `none` as a valid value).

## 2. Review modes
`policy.review_mode_for(changed paths)` already gives fast/batch/careful.
- **careful**: open the PR, do not merge, do not flag needs-human; comment on the issue "PR #x is waiting for your review" and finish `pr-open` (already exists; keep). inspect shows it under review (exists).
- **batch** (contract 8-5): the batch is the issue's **parent issue** (GitHub sub-issue parent; tracker `parent_of(n) -> int | None` via `gh api repos/{repo}/issues/{n}/parent` — 404 → None). Batch branch `foragent/batch-<parent>` created from origin/base if missing (push it). The child's PR targets the batch branch; on Done when + review pass and CI pass (same checks rule) merge into the batch branch with the verified SHA, then the gate closes the child issue itself (`close_issue` with a comment "Merged into foragent/batch-<parent> (PR #x)") because `Closes #` does not fire on non-default branches. When every sub-issue of the parent is closed (tracker `sub_issues(parent)`), open (or reuse) a PR `foragent/batch-<parent>` → base titled "Batch: <parent title>", body listing children and their PRs, `Closes #<parent>`; do not merge it (human). inspect shows it as review.
  - A batch-mode issue with no parent → behave as careful.
  - Blocked-by between siblings works as before (children close when merged into the batch branch, which unblocks the next).
- Worktrees for batch children are created from `origin/foragent/batch-<parent>` (so siblings build on each other), and acceptance blob binding still compares against the approval snapshot (acceptance files come from base; if the batch branch changed them, that is already a hold).
- Tick reconciles manually merged child PRs targeting their parent's batch branch, closes the child and removes ready/running labels before scheduling dependent siblings. Every tick also retries final PR creation for completed batches, using merged child PRs to discover the batch even after all children have closed. Reuse an open final PR; never recreate a merged final PR.
- Path overrides replay child patches with `git apply --3way`; conflicts fail the attempt and retry implementation on a clean destination tree within the existing retry budget. Git text output uses replacement decoding, while exported patches preserve source bytes.

## 3. Stall detection and one restart (tick side)
- Runner updates `last_progress_at` whenever the executor log grows (poll the log size every 30 s while the executor runs; inject the clock and a poll hook for tests) and after each step.
- Tick: a running run whose `last_progress_at` (or `started_at`) is older than `stall_minutes` and whose process is alive → kill the process group (existing helper), clean the worktree, `restarts += 1`; if `restarts` ≤ 1 → label back to `ready` (it will be relaunched by the normal start step, preserving `restarts` in the next run record via store); else flag `stalled`.
- Persist stall count/outcome before termination. Roll back only if termination fails, retaining the count if cleanup fails. Recovery of a dead active run completes its persisted outcome instead of overwriting it with `crashed`.
- Reference for the approach only (do not copy code): orwa nightshift `plugins/nightshift/hooks/clock-out-gate.sh` around lines 358–366 (clone under `/private/tmp/claude-501/-Users-apple-Dev/b2c69c16-6848-43ed-8fcb-6fc67bc3da0f/scratchpad/nightshift-scan/repos/orwa-nightshift`, MIT).

## Tests
Reviewer PASS/FAIL/no-verdict/edits-discarded/disabled; FAIL feeds findings to the next implementer prompt; review_max exceeded → PR + risk-high hold; careful path; batch: child PR targets batch branch, merge closes child, sibling unblocking, final batch PR opened once when all children closed, no-parent fallback; stall → restart once → second stall flags; progress updates prevent false stalls.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
