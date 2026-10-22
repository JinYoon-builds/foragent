# Baseline survives tests renamed on main (D53 addendum)

Found 2026-10-22 during order #99: a test renamed on main (commit fdfe83c) made four running issues
fail every attempt with `ERROR: not found` from the acceptance-baseline verification.

Rule: baseline node ids that no longer exist in the branch tree at verification time are dropped and
logged; surviving baseline tests keep D53's guarantee; an empty remaining baseline skips verification.
This is a fact check (does the node still exist), not a judgement.
