---
name: project-prompt-context
description: #1026 planning-turn box facts — UserPromptSubmit hook, checkout binding, tolerant box scan, notice record shape, statusline question count
metadata:
  type: project
---

- Hooks live in `statusline.HOOKS` (event → fa args + group). `has_hook` means *all* installed; `with_hook` adds missing ones; `without_hook` removes all. `fa statusline --prompt-context` (timeout 5 s) prints `hookSpecificOutput.additionalContext`; partial reads go to stderr and into the context.
- Box is bound to one checkout: `<state>/checkout.json`, single writer `presence.bind_checkout` (called from `init_repo` end and the SessionStart hook, which also runs `_sync_hook` so old installs gain the prompt hook without `fa init`). Reader `prompt_context` needs bound root == project root and files-origin == repo.
- `orch_inbox.scan(store, kind)` isolates corrupt rows (needs dict, str id, int seq, str status) and takes no lock; the prompt path must never call blocking flock (acceptance test patches fcntl.flock).
- Notice = journal event `kind:"notice"` via `orch_inbox.record_notice`; read with `recent_notices` (256 KiB tail, 24 h, last 10). #1030 writes them.
- Quote every outside string (ids, file names too) with JSON in agent context. Defect class seen: unquoted outside input.
- Base has 4 failing `test_presence_session_start` squirrel tests (bottom-only cell must be `▄`). They belong to the lane-m2-art order (`test_lane_m2_art`), not #1026: review rejected changing `watch_art.ansi_rows` here as out of scope (session behavior), even though an orch hint asked for it. Leave other issues' failing art tests alone.
- Review defect class: incomplete corrupt-row isolation. Every presence read of the box must go through `orch_inbox.scan`; `counts()` used `waiting_issues` → strict `_records` and one corrupt question hid the whole statusline and session-start line. Regression: `tests/test_presence.py -k corrupt_question`.
- Verify: `fa test tests/acceptance/test_presence_order_stall.py tests/test_presence.py tests/test_statusline.py tests/acceptance/test_presence_session_start.py tests/acceptance/test_statusline_uninstall.py`.
- Review defect class: failed reads shown as empty. `Path.glob` and `is_dir()` hide PermissionError/NotADirectory, so `scan` uses `os.scandir` (only FileNotFoundError = empty). The state folder check uses `os.stat`, and a corrupt `checkout.json` is noted in problems. Regressions: `tests/test_presence.py -k "unreadable or corrupt_binding or missing_box"`.
- Review defect class: unencodable outside text (lone surrogate `\ud800` from a JSON escape, or a surrogateescape file name) raised UnicodeEncodeError only at output and hid every row. Fix: `orch_inbox.clean` in `_quote`/`_name`, per-row render isolation in `prompt_context`, and `errors="replace"` at the single emission point in `statusline.prompt_context`. Tests write bad rows straight to disk, because the writers refuse surrogates. Regression: `tests/test_presence.py -k "surrogate or cannot_be_rendered"`.
- The runner may recreate the worktree from the branch. Committed work survives, but `.foragent/TRY.md` and `REBUTTAL.md` are lost and must be rewritten. In a worktree `.git` is a file, so keep scratch backups in `.foragent/`, not `.git/`.
- Review defect class: narrow per-row except lists. `json.loads` of deeply nested input raises RecursionError, which is not a ValueError. Box/journal/binding/hook-input parsers catch `Exception` per row, and `stalled` isolates each row. Regression: `tests/test_presence.py -k "deeply or cannot_be_computed"`.
- Review defect classes: silent truncation and check-then-write races. A journal tail that does not reach back past `since` is reported, and the notice limit and text limit name what they left out. `bind_checkout` re-checks and writes under `store._state_lock()`. That lock is flock-based and also blocks between threads, so the race test uses threads and events. Regressions: `tests/test_presence.py -k "journal_tail or over_the_limit or at_once"`.
- Rule from 2026-10-31, after two holds on one class (box rows handled as raw text):
  - Read JSONL by splitting the bytes on `\n` only; `str.splitlines` also splits on U+2028/U+2029.
  - `json.loads` every line, then filter and sort on parsed fields only; a raw `'"notice"' in line` check misses a kind written with an escaped letter (backslash-u-0069 for the i).
  - Skip a bad line on its own with one diagnostic.
  - For a tail read, seek to `start - 1` and drop the first piece: it is empty when the tail begins at a line start.
  - Regressions: `tests/test_presence.py -k "escaped_kind or line_separator or raw_notice_line or truncated_last or exactly_at_its_edge"`.
- The spec asks to state design choices in the PR. TRY.md becomes the PR body, so add a `## Design choices` section after the required headings.
- Review defect class: incomplete schema validation.
  - `scan` checks per-kind field types (`orch_inbox._ROW_FIELDS`), and notices are checked with `NOTICE_FIELDS`. Each diagnostic names the wrong fields.
  - A JSONL line that is not an object is diagnosed, not silently dropped.
  - Optional fields may be absent (older rows), but a field that is present must have its type.
  - Regressions: `tests/test_presence.py -k wrong_typed`.
- The journal reader checks every event against `JOURNAL_FIELDS` (at, kind, summary, issue, say), not just notices. A `kind: []` or a missing kind beside a valid notice must give a diagnostic. Both journal writers (`append_event`, `state_phases`) produce this shape. Regression: `tests/test_presence.py -k "malformed_journal_event or other_kinds"`.
- A "baseline regression" in another lane's acceptance test can be a test-owner fix. Here `test_statusline_wrapper.py` (fixed on main in 560dbcb6) ignores every `fa statusline --…` hook. When the orch hint says "rebase onto main", first check `git log HEAD..origin/main` and `git merge-tree`: the runner had already merged the fix, and the rules forbid committing.
- `test_facts_baseline_trend_repeated_json_and_human_observation_is_read_only` fails now and then under machine load. #173, #308, #1014 and #1026 were all held on it, and it passed on rerun every time. Check `runs/issue-*-stop.txt` for earlier verdicts, rerun it, and rebut; do not change facts code from the view lane.
