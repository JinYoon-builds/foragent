# Review verdict handling with D44 finding handoff preserved

## Goal
Every completed review round records its verdict. Output lacking a verdict line never spends the defect budget, causes the three-round risk-high defect hold, or justifies “Reviewer still found defects”. Preserve D44: free text naming findings may go verbatim to the next implementer as rework. A verdict-free reply naming no finding, such as `(none)` or an empty result, is not implementation rework: re-run that review once or treat it as no findings, and record the affected round as UNUSABLE. Native Claude receives the same exactly-one VERDICT: PASS / VERDICT: FAIL contract as Codex. Real explicit FAIL still records findings and blocks.

## Diagnosis and responsibility
The defect-budget counter in src/foragent/run_review.py:526-540 charges every non-PASS, including UNUSABLE. src/foragent/run_publish_flow.py:110-118 then describes exhausted malformed replies as defects. Native Claude gets an empty prompt at run_review.py:208 and a temporary scope at :142-146 that omits the verdict contract; executors.py:298-309 selects native /code-review without stdin. These are tool/context-delivery defects.

Whether free text names a meaningful finding is an agent judgment, not a new scripted classifier. The reviewing/handling agent needs the explicit verdict contract and the current distinction between unstructured findings and no-finding replies. Preserve raw evidence and D44 handoff; do not introduce keyword lists, semantic regexes, similarity thresholds, case-specific meaning branches or a scripted LLM classification decision. The lane chooses the tools, context delivery, representation and recovery; the order freezes observable outcomes only.

The earlier draft incorrectly prohibited all missing-verdict rework. That was a planning judgment error. This revision narrows that prohibition to replies naming no finding; it does not repeal D44 or require changes to older locked tests. Effort values and model choices are not acceptance criteria in this narrowed order; the verdict contract must reach the agent actually doing native review.

## Observable acceptance
- A missing verdict does not consume even a zero defect-review budget. Three missing-verdict finding handoffs also do not consume the ordinary two-failure budget or produce a risk-high defect hold. Each missing-verdict round is recorded as UNUSABLE, with no counted defect findings; the raw finding remains available to the handling agent.
- The exact finding used by tests/acceptance/test_orch_review_handoff.py::test_unusable_review_goes_to_next_implementer_without_a_hold still reaches the second implementer verbatim. The implementation is reviewed again, and the scripted later PASS permits merge. Two implementation calls, attempts=2, and no blocked/needs-human remain required by that older test.
- In an otherwise green run, a no-finding missing-verdict response causes no implementation rework. Re-review once or continue treating it as no findings; retain an UNUSABLE round record. No transient stop or blanket ban on merge is imposed for this no-finding case. This also covers a no-finding perspective or authoritative merger response.
- Both Codex and native Claude actual review contexts state the same one-line verdict contract. Valid PASS/FAIL results must be consumable by the selected transport. No change to a shared prompt alone counts if native Claude never receives it.
- A real explicit FAIL with a concrete finding still blocks merge and has one recorded FAIL/finding effect.
- The review pipeline remains the single writer of round records. No lost or duplicate records.

## Scope and compatibility
One size-s review-engine issue repairs review accounting, agent context delivery and no-finding handling, while preserving unstructured finding handoff. Keep tests/acceptance/test_orch_review_handoff.py byte-for-byte unchanged. Both locked files are in Done when so neither can be satisfied by violating the other. No older-test reconciliation or reapproval is required by this order.

## Locked acceptance
Publish the supplied tests/acceptance/test_review_verdict_line.py. It calls public run_issue with scripted reviewer transports and existing offline Git/tracker fixtures. Its source is embedded in acceptance_files. The tests use known fixture judgments; they do not prescribe a production meaning classifier. Break-it covers false verdicts, absent/duplicate records, dropped finding handoff and empty-output rework.

## Done when
```sh
uv run pytest -q tests/acceptance/test_review_verdict_line.py tests/acceptance/test_orch_review_handoff.py
```

## Out of scope
Implementing the fix during preparation; editing any existing locked acceptance file; new effort thresholds or model policies; quota or perspective scheduling redesign; live models or network checks; broad suites or pytest-xdist; Git commits or actual publication in this run.
