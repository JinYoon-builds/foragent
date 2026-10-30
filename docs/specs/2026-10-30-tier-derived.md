# #238 derived tier (2026-10-30)

Architect r42: the hold at run_implementation.py:141 and earlier rounds all came from turn identity. The tier is now a pure function of the turn count k = len(attempt_tiers)+1 and the base tier read once at turn 1; no floor or resume rule. Locked tests unchanged.
