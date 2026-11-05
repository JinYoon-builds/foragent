# Foragent mechanisms

The docs owner keeps this map of what runs now, where its code lives and why it exists.
Start here when contributing, planning, proposing structural changes or reviewing a change.
The docs owner rewrites entries in place per merge group. History stays in
[decisions](decisions.md) and the linked issues; this map describes the current paths.

## Merge: a checked PR goes to the merge owner

In this repository, the worker finishes its checks and review, publishes the PR and waits for
its CI evidence before handing it to the merge owner. The admission survives the worker's exit.
The scheduler starts a separate owner process without waiting for its network calls or checks,
so a slow merge does not stop it from starting other work.

The merge owner takes queued PRs, fetches their target and refreshes a reviewed branch when that
target has moved. It verifies the refreshed head and waits for its own CI evidence. Just before
merging, it holds the merge and main-write locks and reads the approval, issue, PR head, target,
policy and evidence again. It merges only the checked head. Failed checks return the work for
repair; a lost response is reconciled against the PR before another action.

This repository has `merge_owner: true` in `.foragent.yml` (switched on 2026-11-04).
The product default remains off. This repository also enables `merge_batch`: the owner builds
a combined candidate for eligible PRs, checks its tip with CI and each member's completion
commands, then repeats the final checks and updates the target once if its base is unchanged. PRs that
change CI workflows take the single-PR path. The worker-side merge-group path still exists
for configurations with the owner off.

Code: [handoff](../src/foragent/run_publish_flow.py) (`src/foragent/run_publish_flow.py`),
[reviewed PR handoff](../src/foragent/run_publish_reviewed.py) (`src/foragent/run_publish_reviewed.py`),
[owner](../src/foragent/merge_owner.py) (`src/foragent/merge_owner.py`),
[combined candidate](../src/foragent/merge_owner_batch.py) (`src/foragent/merge_owner_batch.py`),
[final reads](../src/foragent/run_publish_merge.py) (`src/foragent/run_publish_merge.py`).
Origins: #1797 (owner), #1502 (switch-on and measurement), #1970 (one tip CI),
#1989 (workflow changes take the single path); D125 (switch-on), D118 (sole main writer),
D120 (whole-batch merge) and D95 in [decisions](decisions.md).

## Verification: prove this change still works on its target

The runner executes the issue's Done-when commands and protects the acceptance checks that passed
before the change. In CI mode, it uses CI reports tied to the relevant commits for that comparison;
checks marked local still run on the machine. A moved target needs evidence for the new combination.
On the worker-side group path, a combined check can cover several ready changes instead of
repeating the same work for each one.

Code: `src/foragent/run_checks.py`, `src/foragent/run_checks_approved.py`,
`src/foragent/ci_mode.py`, `src/foragent/run_group_ci.py`, `src/foragent/run_group_check.py`.
Origins: #1403 (combined CI evidence), #1415 (group size from results); D53 and D81 in [decisions](decisions.md).

## Orders and approvals: agree on the result before work starts

The planning session turns the person's request into an order with child issues, scope and
commands that demonstrate completion. A lane drafts acceptance checks before approval locks
them. The approval records the agreed issue and acceptance files, so an implementation cannot
silently change what passing means. The planning session decides what to build; the lane chooses
how to implement it within that agreement.

Code: `skills/fa-order/SKILL.md`, `src/foragent/order.py`, `src/foragent/scheduler_approval.py`,
`src/foragent/run_checks_approved.py`.
Origins: #1570 (whole authored issue reaches workers and reviewers), #1679 (approval across retries);
D98, D100 and D103 in [decisions](decisions.md).

## Lanes and capacity: start independent work when resources allow

The scheduler selects approved issues for the lanes that own their files. Independent changes
can run together in separate worktrees; overlapping files and real dependencies make work wait.
Machine load, memory and executor quota guide admission. Waiting phases release machine slots
and acquire them again before doing work, so a network wait does not occupy a worker's place.

Code: `src/foragent/scheduler.py`, `src/foragent/scheduler_start.py`, `src/foragent/capacity.py`,
`src/foragent/overlap_files.py`, `src/foragent/run_waits.py`.
Origins: #1447 (release slots while waiting), #1526 (record hold causes);
D21, D60 and D87 in [decisions](decisions.md).

## Review: find defects against the agreed goal

Reviewers receive the goal, scope, completion commands and the change. The first review can
use several perspectives; later rounds confirm the original defect classes across all instances.
With review scoring enabled, separate scoring checks the evidence behind each failure.
Structural suggestions remain notes for the orchestrator. A different valid implementation
does not fail review just because a reviewer would have written it differently.

Code: `src/foragent/run_prompts.py`, `src/foragent/run_review.py`, `src/foragent/review_scoring.py`.
Origins: #1016 (goal rather than taste), #1170 (class-wide confirmation), #1599 (scope after PASS),
#1651 (scored findings); D98 and D105, including its 2026-11-02 addendum, in [decisions](decisions.md).

## Planning and the orchestrator: one conversation, durable requests

People enter through the planning session's single command. It reports the project and routes
the next choice to setup, planning or inspection. The background orchestrator handles running
work and leaves questions for planning. Requests and answers stay in the inbox across restarts,
so a session message is not the only record of a handoff. An inbox question alone sends no
direct push to the person; planning carries the conversation.

Code: `skills/fa/SKILL.md`, `skills/fa-orchestrator/SKILL.md`, `src/foragent/orch_inbox.py`.
Origins: #1489 (questions go through planning), #1801 (requests reach the PM's next turn);
D59, D76 and D80 in [decisions](decisions.md).

## Facts and the architect: measure before proposing a change

Facts collect a frozen view of the project's recorded activity and machine inputs. They show
hot files, waits, spend and scope drift so the architect can identify a structural problem
with evidence. Role signals record a burden and compare later facts to the recorded baseline.
The architect proposes a measurable improvement; the orchestrator judges it and routes person
decisions through planning. Finding nothing worth changing is a valid result.

Code: `src/foragent/orch_facts.py`, `src/foragent/orch_facts_machine.py`,
`src/foragent/orch_role_raise.py`, `src/foragent/orch_architect.py`, `skills/fa-architect/SKILL.md`.
Origins: #998 (spend and drift), #1469 (signal burden and calls), #1733 (human-wait evidence);
D82, D93 and D95 in [decisions](decisions.md).

## Order completion: wake the roles with new input

When every child of a published order has merged or closed, the gate records its completion.
New merge or run records can then wake the architect, the docs owner and lane dreams, and
request a retrospective. A session keeps its input cutoff; records arriving during it remain
available for the next review. This gives roles a concrete body of work to examine.

Code: `src/foragent/orch_role_schedule.py`, `src/foragent/orch_dream.py`.
Origins: #997 (order-finish reviews and dreams), #999 (retrospective notes);
D99 in [decisions](decisions.md).

## Memory: keep lessons without turning them into task state

Lane workers append durable lessons to pending memory. The lane's dream folds those lessons
into its current memory and records which ones it consumed. Role sessions have their own
memory directories; the publisher commits their edits locally before trying the remote and
retries publication later if needed. Requests, decisions and waits remain in their work records,
where another session can continue them.

Code: `src/foragent/lane_memory.py`, `src/foragent/orch_dream_worker.py`,
`src/foragent/orch_role_memory.py`.
Origins: #1699 (role memory publication), #1231 (dream redo);
D75, D95 and D102 in [decisions](decisions.md).

## Docs: keep one current explanation and separate decision history

The docs owner repairs flagged explanations, removes stale material and merges duplicated
facts. It owns people-facing docs, this map and the explanatory docs sections of skills.
Skill procedures, agent memory, issues and tests have their own owners. This map is rewritten
in place per merge group, while decisions are appended to their history with a date and reason.
Current rules link back to those decisions rather than repeating their history.

Code: `skills/fa-docs/SKILL.md`, `src/foragent/orch_role_schedule.py`.
Origins: #1472 (one mechanism map), #997 (docs review at order completion);
D108 in [decisions](decisions.md).
