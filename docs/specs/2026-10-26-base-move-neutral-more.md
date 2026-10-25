# More neutral base moves (diagnosed 2026-10-26)

base_move_is_neutral treats a move as neutral only for docs/ paths or locked acceptance files this run's baseline excluded. So every .foragent.yml change (even max_runtime_minutes) and every order publish (its new locked test did not exist at baseline time) forced every running PR into a local re-verify plus a new CI round. Data: 108 main commits in 30 h, at least 25 only docs/, tests/acceptance/ or .foragent.yml; 72 re-verifies vs 42 reuses in the run logs.
