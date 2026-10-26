# Multi-perspective review for risk-map PRs (2026-10-26)

Jin approved this order on 2026-10-25, to follow #379 (risk map). On 2026-10-25, #442 took 3 review rounds, #437 4 and #431 4, and each round surfaced a different class of defect. Reviews of PRs that touch risk-map paths now look from fixed perspectives at once.

- review-perspectives: five product-generic default perspectives plus the project's own (.foragent.yml review_perspectives), and review_reviewers (default 1: one reviewer, all perspectives as a checklist). PRs outside the risk map keep today's single review.
- review-parallel: N > 1 independent reviewers in parallel, one round-robin share each, then one merge step that dedupes and keeps only reproducible defects; fa orch reviewers sets N per issue.
- Later, after #333: review facts (findings per round, rounds per merge, findings per perspective) in fa orch facts, so the architect can propose default changes.
