# Keep later review findings within the work since a PASS

## Authority and current decision
Jin's D105 follow-up, architect r261 option A, as revised by the PM's three QA conditions: a review PASS is a fact bound to the exact reviewed head and issue body. For an ordinary change, the same body limits later blocking findings to code changed after that PASS. The PM now adds explicitly marked risk exceptions and one off switch, and replaces the previous main-integration exception: code brought in from main after the PASS counts as later change and can block. The earlier instruction to exclude main's code is superseded, not an additional constraint.

The supplied 36-hour observation reported 60 of 119 failed rounds after a PASS; 74% of findings for unchanged bodies concerned code not changed since that PASS. These are supplied observations, not new measurements.

## Required result
With this behavior enabled and no matching marked risk exception, another review after CI repair, conflict resolution or a gate restart receives the PASS head, its actual review record and the changes since it. While the issue body stays the same, the reviewer may block on later changed code. Findings confined to unchanged code remain durable, nonblocking NOTE records, visible on the PR. A real defect in later changes still blocks, including alongside a NOTE. Changes integrated from main after the PASS belong to this later change scope, including separate edits in the same file and rewritten rebase histories.

An explicitly exempt risk-map entry preserves today's review scope when this change touches one of its paths. That review can block on real defects in unchanged code, despite the earlier PASS. An entry must itself carry the exemption mark. An unmarked all-paths entry used to select first-review panels does not grant this exception, and a marked entry whose paths were not changed does not grant it either. Keep the applicable risk evidence and exemption visible to the reviewing agent.

One repository setting disables the behavior and restores today's full/confirmation review path even when a PASS has already been recorded. The behavior is enabled by default. Disabling it also restores today's conflict-review frequency; it is not a request to skip reviewers, CI comparisons or locked checks.

Any issue-body edit invalidates the earlier PASS for scope, including after reapproval. The next review is full and can block on any code in the issue's full change; stale confirmation-only instructions must not restrict it. A later genuine PASS establishes a new head/body boundary. Restarting the run preserves an applicable PASS.

## Public configuration contract
These are user-facing YAML fields, not private implementation names:
- `.foragent.yml`: `review_pass_scope` is a boolean, default `true`; `false` restores today's review behavior.
- `.foragent-risks.yml`: `pass_scope_exempt: true` on an individual entry grants the exception only if its paths match files changed by this change. Omitted or false is not exempt. Existing incident entries carry the explicit mark; a future unmarked catch-all remains nonexempt.
- `fa risk add --pass-scope-exempt` records that explicit mark for a new entry; leaving it out does not grant an exemption. Listing preserves it.

## Judgment and preserved behavior
All three QA items change the reviewing agent's criteria. The old draft lacked the PM's risk exemption, revised main-integration scope and opt-out. Supply the explicit configuration facts, applicable risk evidence, accepted head, diff and review record. The reviewer decides whether each finding is a blocking defect or a NOTE. Do not classify finding prose with keywords, meaning regexes, thresholds, extra LLM decisions in scripts or prescribed search steps.

CI comparison, locked acceptance requirements, pre-PASS full review, selected perspectives, merged returns, confirmation of first-round defect classes, fresh/resumed lane prompts and reviewer failures keep their current contracts. A clean base update alone does not require an extra review; when another review occurs its diff includes the main code integrated since the PASS. Locked-test conflicts retain their current treatment.

## Acceptance
Use public issue-run and review-only paths with offline Git, tracker, executor and CI fakes. Retain CI repair, real conflict resolution, body edit, restart and pre-PASS controls. Keep the existing main-integration node identities but replace their old exclusion expectation with blocking results and prompt evidence that includes the main delta. Add paired risk cases for a marked matching entry, an unmarked catch-all and a marked unrelated entry, plus the real repository off setting with both reviewers. Assert supplied context, merge/hold outcomes, returned findings and durable NOTE visibility. Do not require a new private API, helper name or persistence shape.

## Scope
One independently verifiable issue on the existing review-engine lane. The configuration and risk-map paths are supporting edits for this same review behavior; they do not require a second independently deliverable issue or a new lane. The draft is local and unpublished. Implementation choices belong to the lane.
