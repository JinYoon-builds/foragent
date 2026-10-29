---
name: project-settings-window
description: #173 lane-view settings window (s key) — package-not-section trick, per-scope get, personal glyph rule, 24-row trap
metadata:
  type: project
---

#173 settings window lives in `watch_sections/settings/__init__.py` — a **package**, so `_sections()` discovery (which skips `ispkg`) never stacks it. `_frame` calls `shown_sections(sections, state)`, which while `state["settings"]` keeps only `squirrels` (header) + the window + `help`.

**Why:** a plain section module that always returns lines (as `test_view_sections` monkeypatches every section to return one line) pushes the frame past 24 rows, which triggers the TIGHT/short redraw and adds extra render calls — the locked "invoke every section in ORDER" check then fails.

**How to apply:** any future overlay/window that replaces sections should also be a package (or otherwise outside discovery), not a new ORDER module.

Other facts from #173:
- `config.set_setting` is the one writer (thin wrapper over `settings_write.set_value`); `commands/settings.py` looks it up as `config_module.set_setting` at call time so tests can patch it.
- `fa settings get` without a key merges each scope's **own file** (team/me/machine) plus the StateStore quota override — not `load_config` priority. The locked test expects user-file `animation: false` even though `.foragent.yml` says true.
- `load_config._personal_glyphs`: a repository `view_glyphs: auto` defers to a user's explicit emoji/ascii (fixtures dump the full Config, so `.foragent.yml` always contains `auto`).
- Adding an item to the usage line makes it wrap at width 80; `tests/test_watch_usage.py` short-screen and `test_watch_squirrel.py` expectations needed the `merge: batch` item.
- `i18n.text(key, language, **values)`: never name a placeholder `{key}` — it collides with the positional argument.
- `_line` collapses whitespace; aligned rows must be built as raw spans `[(row, style)]`.
- `test_settings_runner_tests.py` (3) fails on base: `runner_unit_tests` belongs to another issue.
- Running every test file that mentions "settings" exceeded 10 minutes; stick to Done-when + changed-file tests.
- **`fa test` runs `uv run pytest` with the default `~/.cache/uv`** (detect_test_commands picks it; `.foragent.yml` has no test_command). Under a "no files outside the repository" goal, export `UV_CACHE_DIR=$PWD/.uv-cache UV_OFFLINE=1` before every `fa test` (`.uv-cache` is gitignored). pytest's `tmp_path` also writes to `$TMPDIR/pytest-of-$USER`; set `TMPDIR` inside the repository too. In #173 I missed this, so the goal's third condition could not be proven.
- Retry 2 (2026-10-29): the review asked for (1) any unmapped key during the team y/n prompt to count as n (`watch_terminal.window_key`; not -1/KEY_RESIZE/KEY_MOUSE), (2) the open window to keep its selected row and prompt on short screens (`_frame` records `settings_focus`, `_window` follows it, because the window ignores scroll keys), (3) the source note to depend on *where* the key is set, not on comparing values. Reviewer cycles claude, codex, unset (None, saved with `unset=True`, "the other tool reviews"), none ("no review"), as `resolved_reviewer` behaves.
- With a repo-local `UV_CACHE_DIR`, a recreated worktree's `.venv` may have no pytest. Drop `UV_OFFLINE` so uv downloads into `.uv-cache` (ignored by its own `.gitignore`), and set `TMPDIR=$PWD/.uv-cache/tmp`.
- Retry 3 (2026-10-29): (1) stale config class: `watch.run`'s refresh is a `_Refresh` object whose `.config` the terminal replaces after a save (and it forces the next refresh); the window sets `state["quota_mode"]` after any save so the usage line shows the effective mode before the next snapshot. `status()` (gate.py) reads no window key, which the REBUTTAL notes. (2) overlay layout class: while open, `_frame` draws the header with `animation=False` (one line) and redraws the window with `room = screen height - other rows`; the window keeps tabs and closing lines and fits rows around the selected one with a "위에 N개 · 아래에 M개" line. `render_frame(height=)` and the terminal store `state["height"]` before `_frame`.
