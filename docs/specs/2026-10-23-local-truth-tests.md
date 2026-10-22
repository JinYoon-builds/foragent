# Test owner's correction: acceptance tests follow the local-truth rule

D71: the orchestrator wrote these acceptance tests; they contradicted documented behaviour, so it corrected them
and re-approved the issues. Implementers still never edit locked tests.
- #138's test observed the PR inside the review callback; the default flow creates the PR after review
  (locked by test_skills_review_scope.py). It now observes at merge entry.
- Tests that mutated the fake GitHub directly and expected watchers to see it at once now go through
  Foragent's own write path (flag, escalate, runner) or an initial gate tick; a person's direct change is
  picked up by a refresh 5 minutes later. Test names are unchanged.
