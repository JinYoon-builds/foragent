# D57: Foragent opens the lane view and reinstalls itself

Decision: docs/decisions.md D57 and its 2026-10-22 addendum. A person should never be told to type
`fa gate watch` or `uv tool install --force --reinstall .`; Foragent does it.

## Shared contract
- `fa gate watch --open [--repo-root R]` opens the lane view and returns at once (exit 0).
  The launched command is `<fa> gate watch --repo-root <R>` (never with `--open`).
- Where: `ORCA_TERMINAL_HANDLE` set -> `orca terminal split --terminal <h> --command <view>`.
  Otherwise, if an Orca terminal was recorded by an earlier foreground open and
  `orca terminal show --terminal <h>` exits 0 -> split that one. Otherwise -> a new macOS Terminal
  window through `osascript` (`tell application "Terminal" to do script ...`), chosen by finding
  `osascript` on PATH (no `sys.platform` check). Nothing available -> nothing opens.
- Already open: the interactive view writes its PID to `watch.pid` in the repo's StateStore root
  while it runs and removes it on exit. A live PID there means: open nothing. A dead PID is ignored.
- `FORAGENT_VIEW=off` disables opening entirely (no subprocess, no output). The test suite sets it
  for every test through an autouse fixture in tests/conftest.py so pytest never opens real windows.
- Opening never fails its caller: errors become one short line of output, exit code unchanged.
- The gate runs under launchd with a minimal PATH: record the absolute `orca` path together with the
  terminal handle so the background open can still reach it.
- Triggers: `fa init` (non-dry-run, config has lanes), `fa lanes add`, `fa order publish`
  (not `--dry-run`), `fa orch watch` start, and the gate starting an issue run.
- Reinstall: after `fa orch merge` merges a PR, if the merged base tree's pyproject.toml has
  `[project] name = "foragent"`, install that tree from a temporary worktree (never the user's
  checkout) with `uv tool install --force --reinstall <path>`, then check the installed package
  under `$(uv tool dir)/foragent/lib/python*/site-packages/foragent` equals `src/foragent` of that tree.
  Print a line with `reinstalled ... matches`. On failure the merge stands; print `reinstall failed: <reason>`
  and append an orch event of kind `reinstall-failed`. Exit code stays 0.

## Out of this order
D55, D56, D49. iTerm or other terminal apps. Non-macOS window opening.
