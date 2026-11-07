# unit imports (2026-11-07)

PM q55: tests/test_board_parents_closed.py, tests/test_ci_fallback_reports.py, tests/test_orch_jobs_unit.py and tests/test_orch_retry_guard.py import locked acceptance modules (test_parent_follows_children, test_parent_sync_starve, test_skills_autosync, test_ci_mode, test_state_split, test_one_entrance_seat, test_merge_owner_proto, test_orch_review_handoff). The locked test_unit_helpers_split.py (#1951/#1952) fails on main because of it.
