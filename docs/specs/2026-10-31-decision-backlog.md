# Decision backlog — stocked, unapproved

Jin, 2026-10-31: stock the unordered remainder of D49, D58, D77, D80 and D85. The supplied decisions.md establishes the intended behavior; audit.txt establishes the existing work and gaps. Read-only inspection of the repository's code, docs/decisions.md and docs/specs supports these boundaries. Decision-heading implementation labels alone are not evidence. This is a local plan for review, not approval to publish or implement.

## Gaps and ownership

| Decision | Already present | Missing outcome stocked here | Lane / size |
| --- | --- | --- | --- |
| D49 — shareable cards | D49 itself is unstarted. Terminal presentation exists (#945, #1019), as does the README work (#861). | A team/party card and an automatically produced order-result image that people can share, using actual recorded activity. | view / m |
| D49 — live browser view | The same existing terminal presentation (#945, #1019); no D49 browser view in the audit. | A browser scene showing the real lanes, work and conspicuous waiting states, with the agreed original characters. | view / m |
| D58, supplement 4 | Per-issue non-style skill choice and use reporting (#454); native interview, catalog discovery and skill approval already exist. | Any role can report a capability gap; the orchestrator can consolidate it, distinguish missing context/tools/permission from a missing skill, and manage trusted-source use, unfamiliar-source review, project-authored skills, limited trials and retirement. | skills / m |
| D77 | The local speed/quality scorecard exists (#763). | Observed failures of main tests after merges appear as a quality measure, with comparison, evidence and explicit unavailable observations. | orch / s |
| D80 | Queues, pauses, widths, expiry, fallback and human authorship protection exist (#690); executor/model selection already exists under D74 (#967). | Explicit per-issue executor/model choices and a saved goal with a deadline can be made, inspected and revised through supported controls, with history and existing safety boundaries. | orch / m |
| D85 | Neutral documentation/excluded-test changes already reuse verification: #376, merged PR #407. Publication already isolates its own paths, but still publishes each order immediately. | Approved ordinary orders and planning decisions/ideas can wait for a shared publication boundary; urgent orders can go immediately, without losing or repeating pending work. | order / m |

The view lane owns terminal/status presentation and its entry points; its card/browser file estimates are provisional extensions of that area. The skills lane owns skill management, orch owns scorecard/planning controls, and order owns order publication and its instructions, following .foragent.yml. No new lanes or fixed skills are proposed. File estimates are not required implementation locations.

## Boundaries with existing and open work

D49's images and browser view are separate deliverables and need no artificial dependency. Neither issue remakes the terminal UI (#945, #1019), the existing README work (#861), D65's still-missing README GIF, D27's comparative measurement, or D86's mascot-selection work. D49 follows the real comparison and usage evidence required by D27; it does not invent promotional numbers. Visual direction and mascot taste remain human decisions under D33/D86 and must be settled through the existing design process before approval. No visual choice is approved here.

D58 preserves #454 and the person's fixed style choices. Its initial trusted sources are the ones Jin already named in supplement 4: Anthropic's official collections, pstack and gstack. An unfamiliar source still needs human approval after the orchestrator's substantive review; project-authored skills use normal PR review. General lifecycle support does not redo D79's portable principle-library import or that import's specific before/after adoption note and comparison, already covered by the open decisions/stall/overlap/pstack order (docs/specs/2026-10-31-decisions-stall-overlap-pstack.md). D75's curation issue (#1000) remains separate. D58 supplement 3's unresolved automatic-loading policy is not decided by this stock plan.

D77 adds only the missing main-test quality observation to #763. It neither rebuilds the scorecard nor tightens cross-review policy. A failed main check is an observation, not automatic blame assigned to the latest merge. Unknown or incomplete observations must not become a clean zero. Scorecard reading remains local and makes no GitHub calls; the orchestrator judges significance and causes.

D80 extends #690's controls without replacing #967's executor/model selection. The open measured-file-overlap work in the decisions/stall/overlap/pstack order, D82's order-finish scheduling (#997), and D97's reload/runtime proof (#1001) remain separate. Goals, priorities, diagnosis and resource choices remain agent/human judgments. The existing check loop is not replaced with a mandatory agent step sequence.

D85 changes when ordinary publication happens, not what counts as approved. It does not redo #376/PR #407, merge-group work, verification policy or locked-test rules. Pending approved work is distinguishable from published work; stocking this plan does not itself approve any order. Planning notes stay outside the active shared checkout until publication, as D85 requires. The batch boundary is an explicit human/orchestrator choice, not an invented urgency classifier or a new fixed 30-minute rule.

## Approval and tests

Every issue is stocked unapproved. done_when: ["true"] and acceptance_files: {} are stock placeholders, not proof of completion. The owning lane writes meaningful behavior tests before approval; the placeholder is replaced with those checks as part of the later approval process. No implementation tests are run for this planning task. No issue is published, approved, committed or pushed here.

## D98 and assumptions

D98 means WHAT, observable behavior and scope, without private names or prescribed implementation. The current request and the existing 2026-10-31 spec supply D98 because the decision ledger stops at D97. No AGENTS.md exists in the inspected repository or workspace, consistent with that existing spec; apply the decision principles and current request. Public product terms, PR evidence and provisional Files entries do not pin private helpers, wire formats or a lane's workflow. D58's consolidated lifecycle is one outcome; D80's assignment/goal controls share the existing plan, and D85's orders/notes share the publication outcome, so those remain one issue per decision. The supplied audit is the status snapshot; no live issue-status reconciliation or benefit ranking is attempted.
