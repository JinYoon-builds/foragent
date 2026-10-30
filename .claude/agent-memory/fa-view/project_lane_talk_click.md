---
name: project-lane-talk-click
description: #174 squirrel click → lanes.talk; hit_test/handle_mouse first-argument dispatch, badge placement, pitfalls
metadata:
  type: project
---

- `watch.hit_test`/`handle_mouse` take either a layout dict (old form, used by fixed tests test_acorn_interact and test_lane_view_d76) or a status snapshot `(snapshot, config, root, *, x, y, width)`. The snapshot form renders the heightless `render_frame` itself (`watch_mouse._snapshot_layout`).
- The squirrel hit box is the glyph cell `(0, y, glyph_width)`. The acorn box now starts at `glyph_width`, so they do not overlap. `tests/test_watch_acorns.py` clicks the acorn box's `x0`, so an acorn box that starts at 0 under the squirrel would break it.
- The 💬 badge goes above the lane's **first** row, not above the running row. A folded lane shows its last finished row first, and the fixed test measures from the lane's topmost squirrel. Text mode is width < 44 (`talk.BADGE_WIDTH`).
- status lane rows have `last_issue` (built from `store.finish_times()`, newest finish of any outcome) and `talking`. `active_runs` entries carry `talking` (`Run.talking` is a property, so `asdict` leaves it out).
- `tests/acceptance/test_view_sections.py::...existing_groups...` already failed on base: once every section returns one line, the frame is taller than 24 rows. This is not a regression from a new section.
- The CLI talk call is `lanes.talk(n, message=None, store=, config=, repo_root=root.resolve())`. In the fixed test, `StateStore.for_repo` is patched at class level, so `StateStore.for_repo(config.repo …)` gives the same store.
- Retry fix: a section that must stay visible until a key or a timeout needs `STAYS = True`. Otherwise the 24-row trim drops it as soon as the KEEP lane rows grow (31 queued, unfolded). The live view's short or scrolled screen also needs a fallback: `talk.alert` puts the line in the bottom alert row.
