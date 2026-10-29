---
name: dream-row
description: #190 dreaming lane row — status 'dreaming' per lane, watch_sections/dream.py must still be a section (ORDER+empty render)
metadata:
  type: project
---

- `status.collect_lanes` sets each lane row's `dreaming` from `store.dream_runs()` with status running. The view reads `snapshot.status["lanes"]`; no watch_data change needed.
- `lanes._rows` builds the idle row through `watch_sections/dream.idle_row` (💭/`[~]`, words from `messages/dream.py`, ` · ✓ N` only when count>0).
- Every module in `watch_sections/` is discovered as a section (`test_view_sections` checks ORDER int + own `render`). A helper-only module still needs a unique ORDER and `render` returning `[]`.
- `test_view_sections_existing_groups_are_modules_used_by_the_compositor` already failed on base 60631d70 (lanes rendered twice by TIGHT redraw); not this issue's defect.
- Verify: `fa test tests/acceptance/test_dream_view.py tests/test_watch_dream.py`.
