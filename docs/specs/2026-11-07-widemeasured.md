# wide-first measured (2026-11-07)

Architect r926: overlap_wide.holds matched narrow issues against the wide issue's declared Files, while other overlap holds use the run's committed effective_files. #2321/#2322/#2323 waited on #1514 for src/foragent/messages/watch*.py, which PR #2406 does not change. The test is red on main; a scratch change passes it and the existing wide-first tests (21).
