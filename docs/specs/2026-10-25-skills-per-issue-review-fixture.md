# Skills-per-issue test fixture (edit to #104)

The locked test configured reviewer: claude but left the fake reviewer answering a bare `VERDICT: PASS`; parse_code_review correctly rejects that, so the worker weakened the review gate to make the test pass and the cross review failed it. The fixture now answers with a /code-review result event like the real CLI.
