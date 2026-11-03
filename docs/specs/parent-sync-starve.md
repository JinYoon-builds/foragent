# Bound order-parent reconciliation under the existing GitHub budget

## Problem and confirmed cause

`src/foragent/board_parents.py:follow_children` visits open order parents in ascending issue-number order and unconditionally calls `sub_issues` for each native parent. `src/foragent/scheduler.py` runs this inside its shared optional GitHub budget: OPTIONAL_GH_SECONDS = 10 and GH_CALL_SECONDS = 5. A timeout or exhausted budget exits the loop; the next refresh tick starts at the same lowest number. The parent step ignores child facts and native summaries already held by the listing, so unchanged early parents repeatedly consume the allowance. This is deterministic reconciliation and transport work; no agent judgment, keyword rule or LLM decision is needed.

Before writing this draft, an offline run of the real `follow_children` with 41 parents, a fake monotonic clock, real `gh.Budget(10, 5)` and tracker reads taking five seconds produced reads [1000, 1001] on each of four consecutive ticks. Every tick ended with `gh time budget exhausted`; only 2 of 41 parents were ever reached. Board transport was replaced with a no-op in this narrow reproduction.

Read-only inspection of the retained local journal on 2026-11-01 found 241 `parent_sync` tick-step-deferred events since 2026-10-31 UTC: 133 contained `gh time budget exhausted`, and the others were timeouts. The request reported 134 exhaustion events; that count is not substituted for the observed snapshot. The local issue facts show #1159 merged at 2026-10-31T10:40:05Z and parents #1402, #1414 and #1443 still open with listed native summaries at 100%. There are zero `parent_closed` event records in that window. The current closing branch appends a report action but does not emit that journal event, so event absence alone does not establish that no parent ever closed.

## Required outcome

An order parent cannot starve behind lower-numbered unchanged parents. Within the existing shared ten-second optional GitHub budget and five-second per-call cap, every parent needing action is reached within a finite number of successful refresh ticks. In the locked workload of 41 open parents with only two four-second calls fitting per tick, all three highest-numbered finished parents close within N = 6 ticks, including completed, mixed completed/cancelled, and all-cancelled children. An established parent whose relevant listing and child facts have not changed needs zero per-issue GitHub calls or board calls, including after reloading gate state. Child-only changes must still drive parent action when the parent body and timestamp do not change. Use facts already supplied by the listing and its child observations, including sub_issues_summary, where they establish the result; derive from existing facts before introducing any stored state. A parent with any open work stays open. Rate limits still pause the gate tick, and normal retries after the pause close and comment once. Preserve the closing, comment grouping, cancellation, approval and board behavior locked by test_parent_closes_when_children_end.py and test_parent_follows_children.py. The lane chooses the implementation.

N = 6 is the acceptance bound for the fixed 41-parent workload, starting with the first successful listing refresh after the three highest-numbered parents' children end. The transport takes four seconds per external per-issue or board operation, so the unchanged ten-second allowance can finish at most two such calls per tick. This gives finite slack for child reads, closing and board updates without prescribing a traversal order or private state. The six-tick count excludes a genuine GitHub rate-limit pause or a failed listing refresh. With continued successful refreshes and bounded successful calls, no action-needed parent may remain indefinitely unreached. The general bound may scale with the finite workload; six is not a universal bound for arbitrarily many parents.

The existing listing is the fact source, not just a wake-up signal. An unchanged parent is one whose relevant child facts and native summary also have not changed. A child closure, label change or open PR can require action even without a changed parent body or timestamp. Missing summaries or unreadable references must not become evidence that all work ended. Keep existing behavior for order records and plan-marker references. Store new facts only if the necessary result cannot be derived from existing observations, and explain that necessity in the implementation review.

## Scope

One independently verifiable issue, size s, lane board as defined by `.foragent.yml` for `src/foragent/board*.py`. Expected implementation paths: `src/foragent/board_parents.py`, and focused unit coverage in `tests/test_board_parents.py`. The new immutable acceptance file is `tests/acceptance/test_parent_sync_starve.py`. The implementation owns its approach; this order does not choose a cursor, cache, queue, storage schema or step sequence.

Changing scheduler time limits or tick cadence; changing child completion or cancellation semantics, comment wording contracts, approval handling, board precedence, plan-reference ownership, rate-limit policy or ordinary outbox delivery; adding a second parent-progress source of truth; publishing this draft or modifying existing locked acceptance files.

## Locked acceptance

- With 41 open parents and 38 unchanged lower-numbered parents, the three highest-numbered finished parents close within six ticks under the original shared budget. Completed, mixed cancelled/completed, and all-cancelled work count as ended. All unfinished controls stay open.
- Repeated successful listing ticks and a reloaded StateStore produce zero per-issue or board calls for an unchanged population. Broad issue/PR listings remain allowed. A blanket skip of all parent following fails the completion cases.
- A rate limit on a highest-numbered parent's close must actually be reached and pause the public gate tick. A tick during that pause makes no further per-issue or board calls. After the pause expires, normal ticks finish the parents once.
- After completion, repeated refresh ticks and a gate-state reload neither close nor comment again. Existing locked tests retain detailed comment grouping, board transitions and approved-parent behavior.

The acceptance tests drive the public scheduler tick with the repository's offline fixtures, the real budget implementation, listing summaries and an injected transport clock. FakeTracker's internal nested calls count as one external operation, matching a real tracker method such as `gh issue close --comment`. No production helper or parent decision is replaced.

## Verification contract

Run only these focused commands from the repository root; never run the full suite:

```sh
uv run pytest -q tests/acceptance/test_parent_sync_starve.py
uv run pytest -q tests/acceptance/test_parent_closes_when_children_end.py tests/acceptance/test_parent_follows_children.py
```

The two existing locked files were run before drafting and passed: 24 tests. The three new acceptance cases fail on the unchanged source for the reported behavioral reasons: the finished tail stays open after six ticks, unchanged parents spend calls, and the tail's rate limit is never reached. The new tests become green only once the implementation satisfies this contract; this draft does not implement the repair.

## Assumptions

This order is a local draft, with no issue publication or source edits. The six-tick bound applies to the declared finite workload with established observations, successful refreshes, bounded calls and no active rate-limit pause. Normal global listings and existing outbox retries retain their current behavior. The listed native summary is transport evidence; a percent-complete value never replaces the locked child and plan-reference closing semantics.
