---
name: project-376-base-move
description: #376 result-neutral base moves in run_publish.refresh_base, run_base_move.py classifier, bytecode in checks pitfall
metadata:
  type: project
---

- `run_publish._publish_locked.refresh_base()` asks `run_base_move.base_move_is_neutral()` before merging a moved base. Neutral = every commit in `HEAD..target` (git log -m --no-renames, per commit, not net diff) touches only `docs/` or locked files owned by another approved, unmerged issue (`run_baseline.unmerged_acceptance_owners()`, shared with `_baseline_exclusions`), and none of the branch's own `changed_paths(target_ref)`. Neutral merges skip `_verify_implementation` and do not count toward the 3-merge "base kept moving" budget; a conflict falls back to the old path and counts.
- Pitfall: verification runs pytest in the implementation worktree; in a repo without `.gitignore`, `__pycache__` gets staged and published, so the post-refresh `merge_decision(git.changed_paths(...))` saw `tests/acceptance/__pycache__` and held with acceptance-touched. The fake tracker's `pr_changed_files` is `[]`, which hid it on the first decision. Fix: `run_checks._check` passes `PYTHONDONTWRITEBYTECODE=1` (merged with `capacity.HELD_ENV` under a slot).
- conftest forbids `git commit/checkout/reset/push/stash` in tests; build histories with `commit-tree`/`update-ref`/`read-tree` and a separate `GIT_INDEX_FILE` (see `tests/test_run_base_move.py`).
- The orchestrator hint on retry claimed earlier work was pushed, but no `foragent/issue-376` ref existed on origin; check `git ls-remote` before trusting such hints.
- Review fixes: the publication loop is `while True` (was `range(4)`); result-affecting merges stay bounded by the 3-merge budget and neutral ones by `runner._remaining()` inside `refresh_base`. Locked files count as neutral only if `AcceptanceBaseline.excluded` (new field, set on every `_capture_baseline` return incl. cache hits) skipped them AND another issue still owns them unmerged; an approval made after verification must not hide a test the baseline ran.
- Reusing locked acceptance fixtures from a unit test: `sys.path.insert(tests/acceptance)` then import `publish_case` and its fixture deps (`offline_acceptance`, `isolated_home`) into the module (`tests/test_base_move_publish.py`).
- `tests/acceptance/test_state_records_tolerant.py` fails on base (missing `foragent.records`, another issue).
- Review fix 3: `refresh_base` classifies, injects lane skills and merges the captured `target_sha`, never the mutable `origin/<branch>` ref (another fetch can move it in between). Race test patches `run_publish.base_move_is_neutral` to advance origin and fetch right after classifying.
