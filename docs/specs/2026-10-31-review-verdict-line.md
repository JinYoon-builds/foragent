# Review rounds preserve the verdict contract

## Goal
Every completed review round records one normalized verdict, including unusable output. Missing verdicts are review transport failures, never evidence of implementation defects. Review execution supplies the verdict contract and an explicit supported effort that preserves review coverage, including changed tests.

## Evidence
On 2026-10-31, quota keep-share switches moved the affected runs from Claude implementation/Codex review to Codex implementation/Claude review. Ordinary Claude review uses native `/code-review low`, without the explicit VERDICT prompt. Its adapter accepts literal `(none)` only at the start of the result. Preambles or markdown produce UNUSABLE. The implementation loop counts all non-PASS results against its defect budget, and #1191 received a risk-high “Reviewer still found defects after 3 rounds” hold despite three UNUSABLE/zero-finding records. The problem predates #1169; #1237 only changed the implementer stop condition.

Source evidence: src/foragent/run_review.py:183-213, :228-259, :518-540; src/foragent/executors.py:209-212, :298-309; src/foragent/config.py:63-69; src/foragent/run_publish_flow.py:110-118. Faulty files belong to review-engine (.foragent.yml:89-93).

## Observable acceptance
- Each completed round has one durable normalized verdict. Raw reviewer evidence remains available. An unsuccessful protocol exchange must not be silently promoted to PASS or counted as code defects.
- If a reviewer omits its verdict, the lane may re-ask once, or finish with a recorded transient review failure. Such output never requests implementation rework, spends the defect-review budget, creates a review dispute, or produces a risk-high defect hold. A re-ask may occur within the same recorded round or as a subsequent round.
- This applies to the ordinary reviewer, an independent perspective, and the authoritative findings merger. A merger cannot conceal an unusable member; only a usable authoritative review can permit publication.
- The actual reviewer receives the verdict contract and explicit review effort above low/minimal; model, transport and suitable effort selection remain the lane's choices. Native Claude and ordinary Codex must both meet the contract, independent of which executor quota selected.
- A real explicit FAIL still records findings and blocks publication. Existing merge, approval, capacity and locked-test protections still apply.
- The review pipeline remains the single writer of a round's verdict record; agents supply defect judgments. Do not add prose-meaning keyword lists, semantic regexes, similarity thresholds, case-specific meaning branches or scripted LLM classification.

## Scope
Repair the contract boundary and handling of malformed review outcomes, with related decision/rule documentation. Preserve legitimate agent judgments and the existing authoritative panel merge. Do not redesign quotas, perspective scheduling, unrelated review policies, or the publishing workflow.

## Superseded expectation
The current request supersedes docs/rules.md:51 (D44) only for missing-verdict protocol output. It also conflicts with the old locked tests/acceptance/test_orch_review_handoff.py:140-151, which requires a missing-verdict result to cause another implementation attempt. Record the new requirement in the decision/rule documents. Reconciliation of that older locked test belongs to its order owner through the existing approval path; the implementation lane must not silently edit it. The new focused acceptance command is not a promise that the contradictory old suite passes unchanged.

## Locked acceptance
The order supplies tests/acceptance/test_review_verdict_line.py. It exercises the public run_issue entry with offline scripted reviewer transports. It checks ordinary/native malformed output, perspective and merger failure, reviewer effort/context, recorded verdict effects and an actual FAIL control. The current main baseline is red: 7 failed, 1 passed. In-memory wrong-verdict, skipped-record and duplicate-record mutations each fail the passing control.

## Done when
```sh
uv run pytest -q tests/acceptance/test_review_verdict_line.py
```

## Out of scope
Implementing a fix during order preparation; changing existing locked tests without their owner's approved order; live models or network validation; changes to quota policy; broad test runs or pytest-xdist; publication or Git commits in this preparation.
