---
name: project-launchd-reload
description: #1001 launchd reload recovery — D97 osascript wrapper helpers, restart's 3-bootstrap order, reload fact file, pending retry path
metadata:
  type: project
---

- D97 wrapper: `launchd.wrap_arguments(command)` gives `[/usr/bin/osascript, -l, JavaScript, -e, JXA, *command]`. The command follows the script as `run(argv)`, so `--repo-root` parsing (installed_jobs, gates._plist_gates) still works. `launchd.gate_command(args)` unwraps. Anything that reads `ProgramArguments[0]` as the fa executable must use `gate_command` first.
- Staleness (`needs_environment_refresh`) does NOT include wrapping: the fixed `test_versioned_install` expects an unwrapped plist with the stable shim to be current. Wrapping is applied only when a definition is written (install, restart candidate/rollback).
- `InstalledJob.restart`: build and validate candidate + rollback before bootout → one bootout → candidate, candidate, rollback (max 3) → `state()` check → write `<state>/launchd-reload.json` (at, error, attempts, recovered, loaded, pending, rolled_back, label, plist). Raises if not loaded or if only the rollback loaded. Unit tests that list exact launchctl calls now end with an extra `print`.
- `launchd.reload_pending(job)` lets `update_check._restart` and `_jobs_to_refresh` retry an absent job. Check pending before `job.state()`: tests stub `state` with a dict of only some plists (KeyError otherwise).
- The JXA was verified by running osascript locally with `/bin/sh` (quotes and exit code preserved). Never run launchctl. Verification: `fa test tests/acceptance/test_launchd_reload_recovery.py tests/test_launchd.py tests/test_gates.py tests/test_update_check_unit.py tests/test_tool_install.py tests/acceptance/test_update_check.py tests/acceptance/test_versioned_install.py tests/acceptance/test_reinstall_race.py tests/acceptance/test_installed_gates_visible.py` (168 passed).
- Review round 2 (#1001), three defect classes: (1) every pending-state consumer must read the reload fact, not only `pending_restart` — `update()` restarts `reload_pending` gates without reinstalling when the release is current; (2) after any bootout, every failure (atomic `_write_plist`, bootstrap, unexpected exception) is caught and recorded, in both `restart` and `install`; (3) `_reload_record` fills `error` when confirmation says not loaded, and `install` now confirms with `print` too (unit tests list that extra call). `_reload_lines` shows a pending fact even without error text.
- Never `cp` scratch copies to `/tmp` during break-it checks; keep backups under `.foragent/` (goal forbids changes outside the repo).
