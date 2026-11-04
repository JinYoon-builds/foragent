# #1469 — Direct worsening alarms; role calls start even with full lane slots

Status: **unapproved draft**, rebased 2026-11-03 on main
`43a39c7b1b6edcfdaee18f0a87b606a720b889a8`. This edits the existing open issue;
it creates no replacement issue and grants no publication or implementation approval.

| Order card | Value |
| --- | --- |
| Issue | #1469 |
| Lane | roles |
| Size | m; no measurement justifies another size |
| Goal | A numeric alarm starts an independent architect dive, and the orchestrator receives its result. An answered alarm calls again only for worse comparable numbers. Role calls start even when lane run slots are full. |
| Out of scope | An always-on architect, blocking the orchestrator, short fixed question timers, scripted diagnosis, publishing or approving this draft. |
| Fixed style | No change; the lane selects relevant tool and review skills. |

## Agreement and evidence

Jin's 2026-11-01 decision, confirmed through the PM on 2026-11-02, settles the
behaviour. The gate calls the architect directly when the queue stalls, holds repeat,
slots remain idle despite startable work, or the orchestrator waits on a person.
The orchestrator receives the result rather than a request to authorize that dive.
This confirmation preserves meaning; it is not first-hand publication approval.

The supplied incident says #1004 received `not-now` three times while its review
rounds grew from 3 to 18. The supplied second incident says the same cause fired
eight times in two hours as raw `lane_concentration` numbers changed. Those are
reported observations, not independently retrieved production records.

Current source explains both failure directions. Some signal comparisons discard
the numeric values and retain only affected membership. Other comparisons use
raw equality, which treats improvement as new evidence. Answers record their
disposition but do not capture the numbers at the answer. The gate currently
asks the orchestrator to call a role instead of starting an alarm dive itself.
Source references and reproductions are in REPORT.md.

A further supplied measurement, **2026-11-02 08:18Z**, reports that
`fa orch architect` stopped immediately because the machine budget was full,
**4 of 4**, while lanes ran. Current main's common role launcher requests the
same slot as lane work with a zero-second wait. That admission boundary can
refuse the very role needed to investigate a saturated factory. The public
reproduction below confirms this on current main; it does not claim access to
the production incident's records.

## Behaviour to deliver

Keep the existing Goal's root-cause investigation, one deep topic, dive ledger,
daily review, other-vendor best model, measurement after a fix, and removal of
the feature if its dives do not change anything. A completed dive presents a cause
with records, code and a reproduction, plus a proposal naming the number that
should change, its target, and how to measure it. Prefer removal or simplification.

A queued stall means work remains and no merge has happened for about three
times the recent measured merge interval. A quiet queue is not that stall.
The gate also calls on repeated holds, idle slots with startable work, waiting
on a person, and the existing before-team-wide-action condition. It starts an
independent session and returns while that session runs. The orchestrator need
not answer an alarm first, and receives the architect's actual result afterward.
No role sends the person a message directly.

A role call starts without refusal or delay caused by full lane run slots,
whether the gate calls directly or a person or the orchestrator requests it.
This applies to the architect alias and the common named-role command, including
other roles. The lane chooses how to avoid slot competition. Separate capacity,
a reserved path or another mechanism is an implementation choice, not an
acceptance contract. Existing non-capacity checks, such as a live session of the
same role or a paused executor, are outside this result.

An answered signal calls again when a comparable measured burden is worse than
when it was answered, whether that answer was `not-now` or a completed dive.
Examples include greater review rounds, PR line count or hold count. Same or
better numbers stay quiet. Improvement followed by a return to the answered
value still stays quiet; compare with the answer, not the previous observation.
An improvement before the answer becomes the answer's baseline: 18 at raise,
16 at answer, then 17 is worse than the answer and must call again.
Elapsed time and collection order alone do not reopen an answered cause.
A running dive is not duplicated on the next tick. Suppression survives a
reopened store.

Code measures and compares numbers. The architect judges causes and remedies.
Do not add a list of signal-specific semantic cases, wording classifiers,
similarity cutoffs, an LLM decision in a script, or a forced investigation sequence.
Internal representation, names, metric representation and recent-window estimator
belong to the lane. A rise in a denominator alone does not establish that a
burden worsened; measurement must expose comparable burdens rather than let a
script guess their meaning.

## Public locks

The plan embeds `tests/acceptance/test_architect_gate_alarm.py`. It uses the real
CLI, recorded run history, existing facts input, worker launch/executor boundary,
and public inbox. Full-slot checks read real resource facts and met signals
through the public CLI. No alarm helper, state filename, private command name, or
fingerprint shape is asserted. Process, tracker and executor boundaries are offline.

| Lock | Observable result |
| --- | --- |
| Stall | Three merges ten or twenty minutes apart, then an empty queue: no dive. Add queued work: no early dive, one independent dive at three intervals, no duplicate while live, result reaches the orchestrator. Two rates reject a fixed minute threshold. |
| Worse after answer | Both a projected hold count and an unprojected numeric signal call after worsening despite an earlier `not-now`; the completed dive covers unchanged numbers. |
| Same or better | Equal and decreasing numbers, reordered members, return to answered values, long elapsed time and reopened stores produce no new call or inbox request. Includes the existing `--no-start` path. |
| Answer-time baseline | 18 at raise, 16 at answer, then 17 calls; comparison against the original raise would miss it. |
| Full lane slots | Four live lane runs occupy a machine budget of four; public facts report zero free slots and the role signal is met. The gate alarm, explicit architect alias, explicit named architect and explicit named docs call each start an independent session, execute the requested role and leave the four lane runs live. Waiting for a lane slot fails the check. The gate still delivers the architect result. |

Each Done when command targets one file and uses the normal runner:

```sh
uv run pytest -q tests/acceptance/test_architect_gate_alarm.py
uv run pytest -q tests/acceptance/test_role_signal_raise.py
uv run pytest -q tests/acceptance/test_role_signal_not_now.py
uv run pytest -q tests/acceptance/test_role_signal_meaningful_change.py
```

The provisional draft's `test_role_signals.py` Done when command is removed:
on clean current main it has 22 passes and an unrelated scorecard failure. Its
process-ban fixture rejects a Git history read because in-checkout temporary
projects inherit the enclosing `.git`. The same failure occurs with the
prototype. All 23 node ids remain unchanged in the current-main file; the file
was still run in full on both variants. This command was never in the supplied
issue body, so it is not an additional `done_when_removals` entry.

The old signal locks still protect pending request bookkeeping under `--no-start`,
affected-entity changes, other roles, explicit answers and scorecard counts.
They are not permission for the normal running gate to ask the orchestrator
whether an alarm dive should start. Their suppression of growing counts in the
request-only path is distinguished from an actual architect call. This compatibility
reading is an assumption; it does not weaken the new running-gate locks.

## Removed text and reasons

Remove the Goal's `Not ready to approve` paragraph because this order supplies
the order card and executable acceptance draft it requested. The issue remains
unapproved; preparing those materials is not approval.

Declare both Done when removals exactly in `edits[0].done_when_removals`:

- `false  # placeholder: acceptance tests are written before this issue can be approved`
  — replace an unconditional failure with the public executable locks above.
- `Write acceptance tests and show the order card before approval`
  — this draft includes the tests and card; publishing and approving them still
  requires the existing review and first-hand approval process.

Retain both reasons in the issue's removal history when it is eventually published.

## Publication limits

The current `edits[]` schema does not accept lane or size fields and preserves
the issue's existing routing labels. This order card requires `lane:roles` and
`size:m`; the supplied body contains no labels, so their current values are
unverified. A publisher must verify that route before publication. The draft
does not invent unsupported plan fields or change the order engine.

Existing acceptance files are read from current main without modification; all
42 current role-signal node ids are retained. The new alarm file is absent on
main, so its six original draft node ids are retained and four full-slot nodes
are added. No acceptance file is copied from an older branch over current main.

The prototype is evidence, not the lane's required design. It is kept only as
`prototype.patch`; it is not applied to the final source tree. REPORT.md records
the tests actually run, the removed command and its unrelated failure, and what remains
unproven. This draft has not passed preread, independent test review, or approval.
