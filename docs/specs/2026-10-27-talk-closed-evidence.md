# Talk closed evidence (2026-10-27)

Real Orca reports a closed terminal handle with exit 1 and a terminal_handle_stale envelope on stdout. The locked fake in test_design_session.py now does the same (stderr unchanged), so #154 can close a talk only on structured evidence.
