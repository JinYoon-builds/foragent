---
name: m2-skeleton-seams
description: #1058 lane-view m2 skeleton seams — REPLACES/PIN in _frame, _Palette pairs, explanation selection, pitfalls found
metadata:
  type: project
---

#1058 (m2 skeleton) delivered the seams that later m2 issues (art, facts, shell, scene, introductions) fill. Spec: `docs/specs/2026-10-30-lane-view-m2.md` (Contracts section).

- `watch_layout._frame` renders **every discovered** section once (even while settings is open), drops REPLACES targets, then applies `settings_window.shown_sections` to the survivors. The review flagged filtering before rendering. Sections are keyed by `id()` because test fakes are unhashable SimpleNamespace objects. `PIN` rows go to `state['pinned_rows']` (both keys always present).
- Every `init_pair` outside the main base pairs 1..7 must check `_Palette.free(index)`. The review flagged named pairs 8/9/10 crashing when COLOR_PAIRS=8. Base pairs stay unguarded because the theme lock expects all seven calls.
- `watch_terminal._Palette`: question/next/separator are lazy at 8/9/10, working and `('art', fg, bg)` are cached from 11. `curses.COLOR_PAIRS` exists only after `start_color()`. The main theme test mocks do not set it, so read it lazily with `getattr(..., 0)`. 8-colour uses the spec's literal `ANSI8` table. Links are written as `\x1b[0m` + full SGR (`38;5;n` even on 8 colours) + OSC 8 + `\x1b[0m`.
- Selecting an explanation (`role`/`squirrel` targets) must not add keys to state when nothing was selected. `test_lane_view_d76` deep-compares state before and after a click, so use `state.pop("explanation_target", None)` and never assign `None`.
- `watch.run` binds `watch_data.collection_root(repo_root)` inside its `read` closure while keeping the five-argument `collect(...)` call that `test_watch_split` locks.
- Verification: the full `fa test tests` reached only 11% in 20 minutes, so I stopped it. Instead I extracted `git archive HEAD src` (plus `_build.py`) into `.foragent/base`, ran the related files with `PYTHONPATH=.foragent/base/src`, and diffed the test IDs. The baseline had about 130 failing acceptance tests from other open m2/presence issues. Delete `.foragent/base` afterwards (it is not gitignored).
