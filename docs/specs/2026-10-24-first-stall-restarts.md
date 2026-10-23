# A first stall restarts again (regression from #277)

Evidence (2026-10-23, #143): the gate saw a first stall (restarts 0 -> 1, outcome `stalled-restart`) and stopped the runner; #143 then stayed `blocked` instead of returning to `ready`. Cause: since #277 the runner's `except SystemExit` (runner.py:2211) adds `blocked` without looking at the recorded outcome; `_restore_stalled` (scheduler.py:306-307) then sees `blocked` and only removes `running`, never reaching the `restarts <= 1` branch that returns the issue to `ready`.

- A first stall returns the issue to `ready` (no `blocked`, no `running`), one `stalled-restart` row, and the next tick starts it again.
- A second stall still ends `stalled` and stays `blocked` for the orchestrator.
- No `crashed` row in either case.
Tests simulate termination with a fake kill_runner; never send real signals.
