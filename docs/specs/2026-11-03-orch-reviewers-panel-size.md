# Unit test orch_reviewers sizes the panel from the effective perspectives

Same shape as #1753. tests/test_orch_reviewers.py imports CHECKLIST, perspective_case and review_offline from tests/acceptance/test_review_perspectives.py and assumes the risk-map panel has len(CHECKLIST) members. With #1651's six-perspective helper (afd1b174) on current main, the effective panel is four defaults plus six configured additions (ten), so 4 of its 10 tests fail (concurrency/coverage, no-verdict, reviewer-and-merge errors, merge FAIL). On main with today's helper all 10 pass. Derive the panel from foragent.review_perspectives.perspectives(case.config) so the helper change can return without breaking main CI.

Measured on 46741a3e, one file at a time under nice -n 15: main 10/10; main + afd1b174 helper 6/10; prototype 10/10; prototype + helper 10/10; prototype + helper + #1651 prototype 10/10. The other unit files importing the helper (test_review_pass_scope_unit, test_review_conflict_paths, test_worker_budget_unit, test_review_notes_unit) pass on main and with the helper.

Also seen: at machine load ~13 the concurrency test failed once on clean main (admission waited on "machine load too high", peak 1 instead of 2). The prototype adds the same autouse calm_machine fixture test_review_conflict_paths uses (capacity._sensor_block pinned to None).
