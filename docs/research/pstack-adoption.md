# pstack principles: adoption baseline and pending comparison (#1028)

What changed: the 22 remaining portable pstack principles join the two
imported in #725. All 24 are listed by `fa skills catalog` as `pstack:<name>`
and are installed one at a time, on demand, with
`fa skills install pstack:<name> --repo-root .`. No lane got a new fixed skill,
and nothing installs the skills automatically. Provenance and edits:
[skills/pstack/README.md](../../skills/pstack/README.md).

This note uses only the existing `fa orch scorecard` and the run records it
reads. It adds no timer, threshold or new measurement. A difference between
the two windows is not evidence that the skills caused it: lanes, orders and
machine load change every day.

## Baseline (before)

Source: `fa orch scorecard --repo-root /Users/apple/Dev/foragent --json --days 7`,
run at `2026-10-30T16:40:44Z`, before this change merged.

Window: "today" is the 24 hours to 2026-10-30T16:40:44Z. "Previous" is the
median of the six 24-hour days before it (2026-10-23T16:40Z to 2026-10-29T16:40Z),
as the scorecard defines it.

| Measure (scorecard field) | Today | Previous (daily median) |
|---|---|---|
| Runs (sample, `runs`) | 38 | 70 |
| Merges (successes, `merges`) | 17 | 23 |
| Merge rate (`merge_rate`) | 44.7 % | 39.3 % |
| Median minutes to merge (`median_minutes_to_merge`) | 27.8 | 44.7 |
| Lead time, minutes (`lead_time_minutes`) | 35.7 | 280.4 |
| Review returns (`review_returns`) | 45 | 0 |
| Spec questions (`spec_questions`) | 6 | 5 |
| Reverts after merge (`reverts_after_merge`) | 0 | 0 |
| Repeated failures (`repeated_failures`) | none | — |
| Unexplained failures (`unexplained_failures`) | none | — |

The scorecard marked these as worse today: GitHub calls per hour, review
returns, spec questions, CI and conflict-resolution phase minutes, person
interventions, slot and merge waits.

Post-merge repairs: the scorecard counts only reverts (0 in both windows).
Fix-up issues opened after a merge are not recorded anywhere it reads, so that
number is **missing**, not zero.

## Skill use (before)

Source: the same state directory's `runs.jsonl`, the `skills_reported` and
`skills_used` fields the runner records from each run's `Skills used:` line
(latest record per run, finished between 2026-10-23T16:40Z and
2026-10-30T16:40:44Z; records start at 2026-10-25T22:34Z, so the first 2.3 days
of the window have no skill data).

- Finished runs: 346; with a skills report: 301; reporting `none`: 289.
- Named skills: `principle-prove-it-works` 11, `principle-fix-root-causes` 1,
  `fa-state-design` 1, `fa-ux-copy` 1.
- None of the 22 new principles could be used yet: none was available.

Run counts here differ from the scorecard's `runs` because the scorecard
counts per day with its own rules; the two are not compared with each other.

## After (pending)

Status: **pending**. Not measured yet; no value is filled in before it exists.

When: once seven full days have passed after this change merges. Run the same
command with `--days 7` at that time and read the same fields from
`runs.jsonl` for the same span.

| Measure | After (today) | After (previous daily median) |
|---|---|---|
| Runs / merges / merge rate | pending | pending |
| Median minutes to merge / lead time | pending | pending |
| Review returns / spec questions / reverts | pending | pending |
| Repeated / unexplained failures | pending | pending |
| Installs and `Skills used:` mentions of each new `pstack:` principle | pending | — |

## Decision (pending)

Record one of these with its uncertainty, in the existing retrospective:

- **Retain**: the new principles are installed and named in `Skills used:`,
  and the scorecard shows no new worse field that the runs connect to them.
- **Change**: some principles are used and others never; drop or adjust the unused ones.
- **Remove**: no run installs or names them after seven days; delete the 22
  folders and their manifest entries.

Uncertainty to state with the decision: one week of runs, different orders in
each window, and self-reported skill use.
