# Fix red main CI: review_conflict_paths panel size

Order commit afd1b174 locked #1651's perspectives in tests/acceptance/test_review_perspectives.py. The unit test tests/test_review_conflict_paths.py imported CHECKLIST as the panel. On main the effective panel is four defaults plus six additions (ten), so 7 tests fail. Derive the panel size from foragent.review_perspectives.perspectives(case.config). A prototype passes on main (14) and with #1651's prototype applied (14).
