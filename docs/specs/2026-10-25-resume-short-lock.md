# Resume path keeps merge.lock short (diagnosed on #429/#450, 2026-10-25)

run_conflicts._resume_implementation wraps the target merge and _resolve_conflicts (an implementer turn plus the full verification) in _merge_lock. #437 shortened the lock on the publish paths but left this path out of scope, so one resumed run with a conflict froze every merge for the length of its resolution (#450 and #196 waited over an hour behind #429).
