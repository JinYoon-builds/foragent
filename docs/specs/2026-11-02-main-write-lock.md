# One owner for Foragent main writes

Draft date: 2026-11-02. Initial baseline: `7de5669d3be4228e3ccfff43f16982350235c02a`.
Refreshed main pin: `ef419f9c59060110a6226679a3c6e55d7895ab5f`.
One choice-free issue, size `s`, lane `skills`. Implements D95; follows D98.

## Result

Foragent has one owner of main writes. Every existing Foragent path that advances
the local or remote main ref participates in the same repository-wide write
exclusion: implementation merges, recovered/reviewed merges, human-authorized
orchestrator merges, immediate order publication, order and note flush including
recovery, and any existing role-memory commit path. Setup and lane publication
must not provide a bypass.

Once the final pre-merge check succeeds, another Foragent main write cannot occur
before that merge finishes. If a publication arrives then, either it waits until
the merge ends, or the merger detects the moved base and stops for verification
against that base before merging. A PR never lands on a base it did not verify.
Base moves observed before exclusive ownership still use the existing refresh
and verification behavior. Ordinary merges and publications continue to complete,
retain their content, and avoid duplicate effects.

The single owner covers the whole final-check-to-merge interval, including the
external merge call. Merely adding another check leaves the same race. Existing
approval, review, CI, merge-turn and publication-recovery requirements remain.
Slow automatic verification and CI waits retain their existing unlocked behavior.

## Evidence and root cause

The defect is an ownership gap, not an agent judgment failure. The agent lacks a
tool boundary that excludes every competing main writer while consuming verified
base evidence. No semantic classifier, agent step order or additional instruction
can supply that exclusion.

At the initial baseline, `run_publish_flow.py:235-259` checks the fetched base and merges
under a state-store lock. `run_publish_merge.py:29-36` acquires that lock alone.
`tracker.py:1591-1602` sends only the PR-head match condition to GitHub. In contrast,
`order.py:888-890` and `order_flush.py:178,191` use a different publication lock,
whose repository-local path is computed in `git.py:655-664`. The two writers can
therefore advance main independently. The supplied 2026-11-02 observation says
main has no branch protection; this draft does not query or change GitHub.

The full writer audit, exact source references, test node IDs and limitations are
in `REPORT.md`. The disposable prototype is evidence of feasibility, not a
required implementation design. The lane chooses private names and structure.

## Locked observable checks

The plan embeds `tests/acceptance/test_main_write_serialization.py`:

- Four cases schedule an immediate order, order flush, note flush, or a real
  role-settings command committing role memory after a successful final check.
- Two cases schedule an order at the final merge boundary of recovered/reviewed
  and human-authorized merges.
- Three controls require uncontended implementation, recovered and human merges
  followed by publication to complete and preserve both results.

These checks use public entry points and Git ref/file results. GitHub and model
execution are fake. Scheduling observes the Git/OS boundaries; checks do not name
or replace production lock helpers or require a lock filename. A safe moved-base
stop is accepted; a successful merge on different unchecked refs is rejected.
Content and effect counts reject a skipped publication, skipped ordinary merge,
lost content, duplicate issue or duplicate merge. Existing locked files remain
unchanged.

The disposable prototype passes these nine cases and the 66 related cases
selected by this order's Done when commands. Broader probes are not all green:
two process/conflict cases also fail on the unchanged initial baseline, and two existing human
merge cases use a Git fake missing the prototype's added transport capability.
These failures remain visible in the draft report; they have not been weakened
or changed. The prototype is feasibility evidence, not a claim of full regression
readiness. Before publication, the lane's preread must account for those fixture
limitations without bypassing the main-write exclusion.

## Scope and planning assumptions

- “Every path” means all existing Foragent main-ref writers sharing the repository
  and its linked worktrees. Independent clones and uncooperative external GitHub
  writers require a distributed/server boundary and are not established here.
- The base branch is configured; `main` names the measured repository's base.
- Role workers currently leave lessons uncommitted. Role settings commands do
  commit their memory directories, so that existing path is included. Lane dreams
  publish a separate PR branch and enter main through the normal merge path.
- Size stays `s` as requested. The prototype's size is not a session-cost estimate.
- `skills` owns the shared Git adapter in `.foragent.yml`. One issue keeps this
  ownership change and its merge callers together; no lane is created.

## Out of scope

New approval decisions; branch protection or credentials; changing merge policy,
review or CI requirements; distributed locking across independent clones; new
memory writers; unrelated process-liveness test fixtures. This is a draft only:
no issue publication, preread approval, commit or push is performed.
