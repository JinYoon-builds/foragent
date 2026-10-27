---
name: project-466-harness-guard
description: #466 test-harness guard — where it hooks, how holds publish, test pitfalls (plumbing git, zsh args)
metadata:
  type: project
---

- `harness_guard.changed_harness_paths(git, target_ref, files)` reuses `GitRepo.changed_paths` (index vs merge-base, `--no-renames`) so renames give both paths and target-only changes are excluded. pyproject compares `[tool.pytest]` + `[project.entry-points.pytest11]` via tomllib on `<mergebase>:path` vs `:path`.
- Hook point: start of `run_checks._verify_implementation` (after `_prepare_target`, before checks). Every head measurement (implementer turn, conflict resolution, base-move re-verify, CI retry) passes there. `run_publish._guard_harness` commits, `_push_branch`, `_open_or_update_pr`, then `flag()` + `needs-human` label and `raise ExecutorStopped("held")`; `run_issue` records the outcome. `_review_only` (used by `orch resolve` too) calls `_hold_harness_change` before `_verify_review_head`.
- **Why:** `flag()` only adds `blocked`; specs asking for needs-human need an explicit `add_labels`.
- **How to apply:** unit tests in this repo must use plumbing git (`write-tree`/`commit-tree`/`update-ref`); conftest asserts against commit/checkout/reset/push/stash. In zsh, `fa test $files` passes one argument — use `xargs`.
- Related: [[project_376_base_move]]
- Review round 2 lessons: run the guard (stage first) before EVERY `runner._check` on the head, inside the verify loop and in `_verify_review_head`, not once up front. A Done when command can generate harness files, and pass 2 would otherwise accept them. Read Git data as raw bytes (`subprocess` without `text=`), because `GitRepo._call` decodes with `errors="replace"`. An absent pyproject must normalize to the same value as a pytest-free one. APFS rejects non-UTF-8 file names, so such tests skip on macOS.
- Review round 3-4 lessons: guard every head measurement structurally. `run_checks._check` calls `harness_guard.before_measurement(worktree)`, and each run path registers a guard for its worktree (`guard_measurements`). Do not chase call sites one by one. Compare TOML values by type: `false == 0` and `nan != nan` in Python. Compare against the *current* `origin/<attempt.target_branch>`, not `attempt.start_ref`, which is pinned for policy and replay and lags after a merged-in target.
- Review round 5 lesson: never judge measured content from the runner's index. `stage_changes` drops `.foragent/`, `CLAUDE.local.md` and local skills. Build a private index instead: `GIT_INDEX_FILE` set to a copy of the real index, then `read-tree -m HEAD` (which keeps stat data), then `add -A`. Diff that against the merge base.
