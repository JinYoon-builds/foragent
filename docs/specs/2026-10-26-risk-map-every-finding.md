# test_risk_map follows #416 (2026-10-26)

#416 changed the review instructions from 'at most 5 findings' to 'every finding' so retries see the whole review; the locked risk-map test still pinned the old wording and failed on main. It now expects the current wording. Found by #533's spec-question.
