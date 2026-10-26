# Tick cost and gate robustness (2026-10-26)

From #564's step times and the day's incidents:
- issue-record-cache: issues.json was re-parsed about 990 times per tick (github_refresh 19.3 s, board_sync 11.3 s of 37 s).
- closed-pr-watermark: the full closed-PR listing timed out at 120 s under load.
- orch-spawn-liveness: a second orchestrator was opened next to a live one that wrote no heartbeat.
- reinstall-race: uv reinstall failed because running processes wrote __pycache__ into the tool directory.
