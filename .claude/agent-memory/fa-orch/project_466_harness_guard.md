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
