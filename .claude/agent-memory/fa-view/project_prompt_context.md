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
