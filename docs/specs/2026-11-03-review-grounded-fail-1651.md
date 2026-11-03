# Review procedure: edit existing issue #1651 (cut false review work)

## Why

Jin decided on 2026-11-02 to adopt the Anthropic code-review plugin procedure
(Apache 2.0) almost as is, and then to cut false review work. The architect
classified the 30 most recent review FAILs (2026-11-02 15:46Z to 23:51Z):

| Kind | Count |
|---|---|
| Real defect that would have broken after merge | 11 |
| Taste or a demand outside the Goal | 9 |
| A defect of the Goal or of a locked test | 9 |
| Code the PR did not change | 1 |
| Repeat of a settled point | 0 |

22 of the 30 came in round 2 or later, mostly "the same kind still remains".
Only one came from the perspective panel, because the panel runs only on the
first round when a risk note exists.

## What this edit decides

1. Scoring in every round. Every FAIL finding, in any round (single reviewer,
   panel merge, confirmation), gets one fresh scoring session. A finding
   blocks only when it is grounded and scores 80 or more. Grounded means one
   of: a quoted line of the approved Goal or Out of scope, a named locked
   acceptance test, or a concrete input that really breaks if this merges.
   Otherwise it is a NOTE: no rework, kept with its score.
2. Spec questions. If the scorer finds the Goal does not decide the point,
   the issue goes to the planning side as a spec-question, not back to the
   implementer.
3. Round 2 and later use the same standard. The confirmation prompt says a
   newly found "same kind" instance is a FAIL only with one of the three
   grounds. The implementer's "fix every instance of the class" hint stays.
4. The panel runs where it runs today: first round, risk note present
   (`run_review._review`). Widening it is measured after this fix.
5. Kept from the earlier draft: the procedure is a skill file
   (skills/review-procedure) with the Apache LICENSE and NOTICE; five plugin
   perspectives plus a fatal-path lens; the plugin's 0 to 100 table and
   false-positive list; below 80 is a NOTE; a fatal path blocks when it
   exists, whatever its rarity; scoring can be turned off.

## Changed from the earlier draft

- Scoring moved from "first full review only" to every FAIL in every round.
- Added the grounding rule and the spec-question route.
- Dropped the widening of the panel to ordinary changes.
- Dropped the structured JSON candidate format for panel members, the GitHub
  one-call lookup for earlier PR comments, the separate scoring and
  perspective model settings and the Claude read-only policy file. They add
  sessions and code without cutting false FAILs. They can be a later order.
- Findings keep today's text format, so locked tests that script reviewer
  text keep their meaning. Fakes that script a FAIL now also answer the
  scoring session with a grounded score.

## Locks

New: tests/acceptance/test_review_grounded_fail.py. It uses verbatim review
texts from real runs: the nine taste FAILs must become NOTEs and merge; three
real-defect FAILs must still fail with any of the three grounds; invented Goal
lines and test files are not grounds; 79 and 80; fatal path by existence;
invalid replies keep the FAIL; a Goal question becomes a spec-question; round
2 same-kind instances follow the same rule; the panel runs only where it runs
today; the skill ships with its Apache note; scoring can be turned off.

Existing locks that script a FAIL are updated only so their fake reviewer
answers the scoring session with a grounded, confident score. Their outcomes
and assertions are unchanged, except where a lock counted the four old
default perspectives.

## Cost

A PASS round costs what it costs today. A FAIL round adds one scoring session
per finding. A first round with a risk note runs six perspective reviewers
instead of four. Scoring sessions run one at a time inside the review.
