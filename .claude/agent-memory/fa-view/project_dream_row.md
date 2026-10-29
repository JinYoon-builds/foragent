---
name: dream-row
description: #190 dreaming lane row — status 'dreaming' per lane; a helper module in watch_sections/ needs ORDER, empty render and GAP=False
metadata:
  type: project
---

- `status.collect_lanes` sets each lane row's `dreaming` from `store.dream_runs()` with status running. The view reads `snapshot.status["lanes"]`; no watch_data change needed.
- `lanes._rows` builds the idle row through `watch_sections/dream.idle_row` (💭/`[~]`, words from `messages/dream.py`, ` · ✓ N` only when count>0).
- Keep the module where the issue's Goal/Files put it; reviewers fail a change that moves it elsewhere even when tests pass.
- Every `watch_sections/` module is a section. Locked `test_view_sections_existing_groups_...` makes each module draw one line; the base's 14 modules use 23 of the 24 plain-frame rows (14 lines + 9 gaps). A new module with a gap pushes it to 25, lanes is redrawn tight and the test fails. A helper-only module therefore sets `GAP = False` (plus unique ORDER and `render` returning `[]`).
- `fa test` runs the worktree code whatever the cwd/PYTHONPATH, so it cannot check a `git archive` base copy. Use `<worktree>/.venv/bin/python -m pytest` with PYTHONPATH=src from the base folder instead.
- Verify: `fa test tests/acceptance/test_dream_view.py tests/test_watch_dream.py tests/acceptance/test_view_sections.py`.
