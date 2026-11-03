# Rework in the approval-to-merge split

## Problem

`fa orch scorecard` splits each merged issue's time from latest approval to merge into
five parts plus unknown (#1527). The PM re-measured it on 2026-11-03 over 10 merged
issues approved after 2026-11-02 09:00Z (1,722 issue-minutes):

| part | share |
|---|---|
| first implementation, review and CI (work) | 37% |
| concurrency cost | 13% |
| machine limit | 6% |
| waste | 2% |
| unknown | 43% |

Of the 738 unknown minutes, 651 are the second or later implementing, reviewing and
resolving-conflict phases inside a run, that is, rework after a review FAIL. The split
counts only the first phase of each kind as work. A later phase needs a recorded cause,
and rework records none, so it falls into unknown. Unknown is then too large to steer by.

## Wanted result

1. The split has one more part, **rework**, in the scorecard JSON (`parts.rework` in both
   windows) and in the human split lines, next to the other five parts.
2. Rework is the time of the implementing, reviewing, checking, CI and resolving-conflict
   phases that start after a review FAIL or a CI failure **of the same run**. The first
   phase of each kind in the approval interval stays work, as today.
3. A run with no review FAIL and no CI failure has zero rework.
4. A second round that no recorded failure caused (for example a new run after an executor
   usage limit) is not rework. Time with no evidence stays unknown. Missing evidence is
   never counted as zero or as rework.
5. A recorded activity cause still wins over a phase name, as today (a recheck caused by a
   base change stays concurrency cost).
6. The parts, rework included, still add up to the total.

Today the runner records each review verdict locally, but not a CI failure. The fix must
keep a local fact for each CI failure, so that the scorecard can tell rework after CI from
other second rounds. Recording stays local and observational: it adds no network call, no
GitHub write and no change to retries, ordering or merges, and its failure never replaces
the run's own result.

## Contract change to an existing lock

`tests/acceptance/test_flow_scorecard_split.py` (#1527) pins the parts to exactly six
names (`PARTS`) and compares one full parts dictionary. This order amends exactly those two
lines: `PARTS` gains `"rework"`, and the expected dictionary of
`test_five_parts_use_changed_facts_and_keep_closed_issue_history` gains `"rework": 0`
(that run has no failure). No test is removed or renamed, and every other assertion stays.
This supersedes the "exactly six" sentence in `docs/specs/flow-records.md`.

## Out of scope

- Reclassifying anything other than unexplained second-round work phases: waits, ordering,
  machine limit, waste and the concurrency causes keep their rules.
- Rounds after an unusable review or a spec conflict, and rounds in a later run: they keep
  today's rule until a separate decision.
- CI failures that happened before this change: no fact was recorded, so their rounds stay
  unknown. Review FAILs are already recorded, so past review rework is reclassified.
- Any change to how runs retry, review, check or merge.
- Docs pages beyond this spec (the docs role follows the merge).
