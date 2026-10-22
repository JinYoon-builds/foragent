# Restore a removed test name

The D55 rewrite of test_claude_sessions.py dropped `test_claude_sessions_codex_lane_keeps_exec_argv`, which already passed and was in running baselines (#162 failed with `not found`). It is restored unchanged with its old helpers renamed. #123 fixes the root cause.
