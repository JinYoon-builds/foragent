---
name: project-420-whole-picture
description: #420 shared cut notice (cuts.py), whole review findings / Done-when failures, parent order context in approval, moved-file notes on base merges
metadata:
  type: project
---
- `foragent/cuts.py`: `[cut: N more chars|lines; full text: <abs>]`, full copies in `<state>/runs/issue-N/` (`cuts.issue_dir`, `dir_for_log` maps `logs/issue-N.log`). Head cuts stop at a line end and put the notice right after, so the acceptance prefix-matcher counts exactly.
- `WholeFailure(str)` (run_attempt) marks failures whose parts are already limited (all failing checks, all review findings); `_implement` passes `failure_limit=None` for it like CIFailure. Raising it in `AttemptFailure` turns it into str; runner reads `exc.failure` to keep it whole.
- Parent context: `Approval.parent_context` (None = unread). `run_parent.parent_context()` freezes it lazily in `_prepare_implementation` via `tracker.parent_issue()` (one `/issues/N/parent` call returns the whole parent); the resume prompt restates it every time, only for bodies with `foragent:plan` marker; re-approval rewrites the snapshot without it.
- Moved files: `GitRepo.moved_by(target)` (-M -C name-status from merge-base) before both `merge_base` calls in run_conflicts, and `moved_between(previous, new target, paths)` (from their merge base) in `_prepare_target` target switches; `attempt.moved_files` is named once in the next prompt and logged via messages `moved_files_log`.
- Stop hook `check_done` reports every failing command; its 2000-char limit uses `cuts.tail` into the claude run dir (`stop-check-<i>.log`). `moved_between` uses `--find-copies-harder` so copies from unchanged files count. main split state.py: Approval lives in `state_records.py`, `save_approval` in `state_local.py`.
- conftest blocks `git commit/checkout` in tests: build history with hash-object/mktree/commit-tree/update-ref.
- Merge lesson (2026-10-26): a stalled-restart WIP commit can flatten an earlier main merge, so `git merge-base HEAD MERGE_HEAD` points at the old main and files the issue never touched conflict. Fix: `git diff <previous main> <WIP>` is exactly the issue's change; per conflicted file run `git merge-file -p <main> <previous main> <WIP>` (files outside that diff take main's copy). This leaves the index untouched. `.foragent/` is gone after a worktree recreate, so rewrite REBUTTAL/TRY.
- main's `run_previous.py` (previous-attempt summary) had the same silent cuts (stop text, `-n50` commits, files list, review tail); they now use `cuts.tail`/`cuts.head_lines`.
- `cuts.head` backs off to a line end only within `LINE_SLACK` (200) chars of the limit; an unbounded backoff kept 28 of 6000 chars for a long QUESTION.md line.
