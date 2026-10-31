# Orders close the issues they replace

Jin decided that approving replacement work also consents to closing the work it replaces.
The current plan schema rejects replacement declarations, so this decision has no place in
an order and publication cannot carry it out. This is a missing explicit contract, not a
request for scripts to infer which work is obsolete.

The person-facing contract is optional `issues[].replaces: [41, 42]` on each new issue.
Numbers refer to existing issues in the current repository. Omission means no replacements.
If several new issues replace one old issue, its single closing comment links all of them.
Approval through `fa order publish --approve` is sufficient consent. Closure uses the
tracker's not-planned state reason (`not_planned`), not completed. Existing discussion stays.
Dry run shows the proposed closures without performing them; draft publication closes none.
A running replacement target or a target also edited or approved by this order causes a
clear refusal naming that target and its conflict, without closing any replacement target.
Republication adds no comment to an already-closed target and succeeds. Reopening is a normal
human tracker action; an unrelated order does not turn the old replacement into a standing
instruction to close it again.

Planning gets one line directing it to list replaced issues in `issues[].replaces` instead
of asking the person to close them. The exact internal design belongs to the order lane.
This feature order does not itself replace a live issue. It contains three small issues in
the order, board and view lanes. Each has its own locked acceptance file and no blockers.

## Locked checks and the wrong implementations they reject

- Dry run: rejects omitted closure previews and dry runs that really close issues.
- No approval: rejects treating draft publication as consent to close work.
- Approved republish: rejects ignored declarations, completed instead of not-planned closure,
  missing successor links, one comment per successor and duplicate comments or errors on retry.
- Running target: rejects closing running work or closing another target before refusing it.
- Edited target: rejects editing and closing the same issue in one order.
- Approved sibling: rejects approving and closing the same issue in one order.
- Manual reopen: rejects closing it again while publishing unrelated work.
- Skill text: rejects shipping the CLI while planning still asks for manual closure.

The tests drive public `fa order publish` with the repository's offline publishing fixtures.
A clean preread uses the existing fake executor. Network calls fail at the service boundary.
The text check reads the public planning skill. Run only the locked file, serially.


## QA classification and settled behavior

These are tool defects against the behavior settled in this request. The fixes use explicit
facts and decisions; scripts do not decide what work replaces other work or who ought to
answer a question. The earlier planning decision to keep cancelled orders open is superseded
by the behavior below. No new person approval is required to draft this revised order.
The reported examples (#150, #922, #458) are planning-provided incident context; this offline
review does not independently inspect those live issues.

### Order parents finish when all children have ended — board / s

The measured gap is that native children are considered finished only when completed,
whereas a not-planned child leaves the parent open with a decision flag. The board and
issue lifecycle must instead agree on whether any child remains open. Once no child is
open, close the parent and put its card in Done. The closing comment names completed
and cancelled children separately. A replacement closure is a not-planned closure and
counts as ended; it does not require order-replaces to be implemented for this issue.
A parent with any open child behaves as today. Empty parents and approved work with its
own sub-issues are outside this change. The next tick is idempotent, including after reload.

Locked checks reject count-only summaries, moving only the card, requiring every child to
be completed, mislabelling cancellations, closing before the last child ends, and repeating
a closure or comment. The open-child control continues through its last-child transition,
so the case also fails on the missing behavior today. Existing all-completed closure already
works; naming the completed children in the comment is the measured gap for that case.
This replaces older checks that deliberately keep a parent open for a cancelled child.

### Questions name their recipients — view / s

The measured gap is that lane rows render a generic question/person state and the status
line counts people and open inbox questions without projecting each question's recipient
and lifecycle. Existing issue labels, spec-question flags, inbox answers and run history
supply these facts. A new store and wording-based classification are outside scope.

The person is the recipient of D71 questions routed through `fa orch ask` or `needs-human`.
Use 진님 / you, or the existing person name if the messages registry supplies one.
An unrouted spec question, or one being handled by the orchestrator, uses
오케스트레이터 / the orchestrator. The config language selects Korean or English.
The lane view associates the recipient with that issue; the compact status line may
show recipient groups. Answered questions disappear, even if an older spec flag remains.
A historical spec-question flag, comment or run record cannot restore a question after
that run was retried or merged. Current unrelated questions must stay visible.

Locked checks use the public lane frame and statusline CLI, in Korean and English.
They reject missing or swapped recipients, labelling every spec question as the person's,
ignoring either inbox or needs-human routing, retaining an answered question, resurrecting
old run questions, and hiding current questions while suppressing stale ones. Only explicit
recorded answers, routing and run changes are used; question text is never classified.

All three issues have `blocked_by: []`. The board test seeds the tracker-visible result
of a replacement closure, so it is independent of the order CLI feature. Implementation
choices remain with each lane. Source code, existing tests and skills are unchanged in
this planning-only work; each lane reconciles the older contracts it changes.
