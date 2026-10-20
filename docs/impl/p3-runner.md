# P3 spec: runner (fast path) + tick-side enforcement

Builds on P1/P2 (read `src/foragent/*.py`). Product rules: `docs/gate.md` sections 5–7, `docs/contract.md` 추가 6–8, `docs/impl/p2-review.md` section 5. This phase makes `fa gate run-issue N` real for the **fast** review mode, and adds two backstops to the tick. Adversarial review, batch/careful modes and stall restart come later (P7) — keep seams for them but do not build them.

English code/comments. Offline tests only: fake executor, fake tracker, temporary git repos (bare origin + clone). The real `codex`/`gh` are exercised later by the maintainer, not by you.

## `src/foragent/executors.py`
```python
@dataclass
class ExecResult: exit_code: int | None; timed_out: bool; last_message: str; log_path: Path
class Executor(Protocol):
    def run(self, *, worktree: Path, prompt: str, model: str | None, timeout_s: int, log_path: Path) -> ExecResult: ...
```
- `CodexExecutor`: `codex exec - -C <worktree> -s workspace-write --skip-git-repo-check --json -o <tmpfile>` (+ `-m <model>` when set), prompt on stdin, stdout/stderr appended to `log_path`, own process group (`start_new_session=True`), killed as a group on timeout. `last_message` = contents of the `-o` file.
- `ClaudeExecutor`: `claude -p --permission-mode auto --output-format text` (+ `--model`), cwd = worktree, prompt on stdin, same logging/timeout handling; `last_message` = stdout tail.
- `executor_for(name) -> Executor`. Command builders must be pure functions with unit tests of the exact argv.

## `src/foragent/policy.py`
- `review_mode_for(paths: list[str], config) -> str`: strictest of `config.review` and any matching `review_paths` glob (`**` supported; order fast < batch < careful).
- `touched(paths, globs) -> list[str]` (glob match helper, `**` aware).
- `merge_decision(*, changed_paths, issue_labels, config) -> Decision` where Decision is one of: `merge`, `hold(reason: Reason, summary)` — hold when any changed path is under `acceptance_dir` (acceptance-touched), matches `always_human` (always-human-path), the issue has `risk:high` (risk-high), or the effective review mode is not `fast` (P3: summary "review mode <mode> not implemented yet; PR left open", reason always-human-path is wrong — add a new reason only if needed; prefer returning `hold_mode(mode)` that the runner reports without needs-human: it leaves the PR open and finishes the run with outcome `pr-open`).

## `src/foragent/runner.py` — `run_issue(n, ctx) -> str` (outcome)
Context bundles repo, repo_root, tracker, store, config, git, executor factory, clock, and a `sleep` function (injectable). Steps:
1. `store.update_run(n, pid=os.getpid())` (already done at CLI entry; keep). Load the approval snapshot; if missing → finish `no-approval`, remove `running`, exit.
2. **Worktree.** Remove a stale worktree at `store.worktree_path(n)` if present (`git worktree remove --force`, then prune). `git fetch` (explicit refspec as in P2). `git worktree add -B foragent/issue-<n> <path> origin/<base>`.
3. **Bind.** Compare the worktree's acceptance blobs (`git rev-parse HEAD:<path>`) with the snapshot. Mismatch → flag `hash-mismatch` (remove `approved`, clear approval — reuse the scheduler helper; move shared helpers into a small module if needed), cleanup, finish.
4. **Prompt** (build from the *snapshot* body only — never refetch the issue, never include comments). Keep it short and fixed:
   ```
   You are implementing GitHub issue #<n> in this repository.
   <Goal section>
   Out of scope (do not do these): <Out of scope section>
   The work is accepted only when these commands succeed (they will be run for you afterwards):
   <each Done when command in a fenced block>
   Rules:
   - Do not modify anything under <acceptance_dir>. Those tests are fixed.
   - If the issue needs a product decision you cannot make from the text and the code, do not guess:
     write .foragent/QUESTION.md with the question, 2–3 options and your recommendation, then stop.
   - Do not commit, push or switch branches. Leave your changes in the working tree.
   ```
   On a retry, append: `A previous attempt failed. Output of the failing check:` + the last 4000 characters of output.
5. **Execute** with `timeout_s` = remaining budget of `max_runtime_minutes` measured from the run's `started_at`. Update `last_progress_at` after the executor returns. Timed out → flag `timeout` ("Too big for one session; split it in order"), cleanup, finish.
6. **Question.** If `.foragent/QUESTION.md` exists → flag `spec-question` with its content as details (truncate at 6000 chars), cleanup, finish `spec-question`.
7. **Changes.** `git add -A -- . ':!.foragent'`; changed paths = `git diff --cached --name-only origin/<base>`. No changes → counts as a failed attempt with output "The session made no changes."
8. **Checks.** Run each Done when command from the snapshot with `/bin/sh -c` in the worktree, timeout 15 minutes each, env inherits PATH. Record exit codes and the output tail. Any failure → if attempts < `max_retries` + 1, increment `attempts` and go back to step 4 with the failure output (same worktree, changes kept); else flag `retries-exceeded` with the last failing command and output tail, push the branch for inspection (`git push -f origin foragent/issue-<n>`, no PR), cleanup worktree, finish.
9. **Commit & PR.** Runner commits (`git -c user.name=foragent -c user.email=foragent@localhost commit -m "<issue title> (#<n>)"`), pushes with `-f`, opens a PR via `tracker.create_pr(head=branch, base=base, title=f"{title} (#{n})", body=...)`. PR body: `Closes #<n>`, a table of Done when commands with pass/fail, the checklist items from the snapshot as unchecked boxes under "Needs a human check", and one line "Opened by Foragent gate (attempt k)". If a PR for the branch already exists, reuse it (look it up; add a tracker method `find_pr(head) -> int | None`).
10. **Merge decision** (policy.merge_decision with changed paths from `tracker.pr_changed_files`). Hold → flag with the reason, PR link in details, finish `held`. Mode not fast → comment on the issue with the PR link, remove `running`, finish `pr-open`.
11. **Merge (fast).** Poll `tracker.pr_checks(pr)` every 30 s up to 20 minutes: `pass` or `none` → `merge_pr(pr, method="squash")` (branch deleted), the issue closes through `Closes #n`; remove `running`; finish `merged`. `fail` → flag `retries-exceeded` ("CI failed") with the PR link; finish `ci-failed`. Still `pending` after 20 min → flag `timeout` ("CI did not finish"); finish.
12. **Cleanup** always (try/finally): remove the worktree; never delete the remote branch except through the merge.
Every flag also removes `running` (P2's `flag` already does). Every exit path calls `finish_run` exactly once.

## CLI
`fa gate run-issue N --repo-root PATH` now calls `run_issue`. Unexpected exceptions: log traceback to the issue log, flag `retries-exceeded` with a one-line summary, finish `error`, exit 1.

## Tick-side backstops (`scheduler.py`)
- If an integrity check fails for a **running** issue, kill the runner's process group (`os.killpg(pid, SIGTERM)`, then SIGKILL after 10 s if still alive) and finish the run `killed` before flagging.
- If a running run exceeds `max_runtime_minutes` + 10 minutes from `started_at` and is alive, kill it the same way and flag `timeout`.
Inject the kill function for tests.

## Tests
Fake executor scripted per attempt (writes files into the worktree, optionally QUESTION.md, optionally touches acceptance files, optionally times out). Cover: happy path to merge; first attempt fails a check then succeeds with the failure output present in the second prompt; retries exceeded pushes the branch and opens no PR; QUESTION.md → spec-question; acceptance file touched → PR opened and held with acceptance-touched; always_human glob; risk:high label; non-fast mode → pr-open; CI fail; CI none → merge; worktree blob mismatch → hash-mismatch; timeout; no changes; prompt built from the snapshot even if the live issue body differs; worktree removed on every path; finish_run called once; tick kills a running runner on hash mismatch and on overtime. Exact argv tests for both executors.

## Done criteria
`uv run pytest -q` passes. Report what you built, and list any step where the spec was ambiguous and what you chose.
