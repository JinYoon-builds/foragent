# Lanes write locked tests during order preparation

Jin, 2026-10-31. Order: `2026-10-31-lane-writes-tests`.

## Why and root cause

Planning currently owns executable acceptance code (`skills/fa-order/SKILL.md` step 5).
`order.preread` gives the lane read-only permission and records unknowns; it cannot author
checks. `order.publish` accepts optional acceptance_files and its approval gate consumes
clean preread evidence, not independent test-review evidence. The first wrong ownership
is therefore preparation, not the runtime gate. Adding another runtime phase would not
supply the planning lane's missing write permission, context or durable review record.

Move test authorship to the lane before publication. Keep semantic judgment in the
writer/reviewer agents. Scripts validate paths/schema, preserve exact bytes, run commands
and record facts; they must not decide meaning with keyword lists, regexes, similarity
thresholds, case-specific meaning rules, extra LLM classifiers or an imposed agent work
sequence. Required writer/reviewer/approval boundaries are the interview's decisions,
not a prescribed internal implementation algorithm.

## Settled decisions

1. Planning writes plain statements of what a person or caller observes, not test code.
   Existing acceptance_files remain supported, including mixed plans.
2. In preparation, before publication, the issue's lane uses the same executor/model
   setup as preread to write tests in scratch. Collected tests must fail on current main
   for the missing behaviour by assertion, not import/collection/syntax error or zero tests.
3. A different session on the configured reviewer executor checks statements and break-it:
   wrong value, skipped effect, doubled effect. A pass supplies acceptance_files. The
   second quality hold for one issue is a QUESTION to planning (the interview's two-hold rule).
4. This replaces preread for statement issues. Approval needs reviewed tests or existing
   legacy acceptance_files with clean preread; dry-run remains available. Statement issues
   cannot be really published unprepared, even without --approve.
5. Untestable/ambiguous statements become QUESTIONs to planning, which asks the person.
   Non-machine-checkable behaviour gets a checklist item plus at least one runnable check.
6. Test writers may write only test files/helpers; production is read-only. Risk guidance
   reaches writer and reviewer. Process-control tests use fake process tables, no real signals.
7. The person approves behaviour/scope once and does not review tests before lock.
8. Implementation is a separate run. Existing Approval hashes, D11 human review when
   locked tests are touched, and acceptance baseline rules continue to protect the work.

## Contracts (public boundaries, not internal helpers)

- Future plans add optional `issues[].statements` / `edits[].statements`: a nonempty
  list of nonblank strings. Its presence explicitly selects reviewed preparation and
  remains present after tests are generated. Legacy plans omit it. Published issue text
  includes the statements without losing or interpreting their meaning. This bootstrap
  plan itself uses today's schema: its statements are in each goal and it supplies tests.
- `fa order prepare PLAN.json --json` reports `{ "issues": [...] }` and updates only
  successfully reviewed statement issues' acceptance_files/done_when in the plan. It
  creates no published issue and starts no implementation. Existing publication accepts
  that exact plan. It leaves legacy issues for the existing clean-preread route.
- `fa order prepare PLAN.json --status --json` returns persisted facts with no agent call.
  Each issue has `key`, `status`, `phase`, `questions`, `holds`, `reason`, `updated_at`,
  `base_revision`, and available `red` / `coverage` evidence. Status values include
  writing_tests, reviewing_tests, waiting, interrupted, question and reviewed; a writer
  candidate can be reported as written while awaiting review. Phase retains the step
  when status is waiting/interrupted. No machine-specific GUI or global status rewrite.
- The preparation-agent handoff is an explicit interface between the writer/reviewer
  issues, using existing plan fields rather than a hidden test manifest. The writer's
  final JSON is `{acceptance_files: {path: text}, done_when: [command], questions: []}`;
  it also writes these candidates to the allowed scratch paths. A nonempty questions
  list reports missing WHAT without promoting files. The reviewer returns JSON with
  `verdict` (pass, hold, question), `reason`, `questions`, `coverage` and `red`.
  Coverage maps statement text to tests and break-it evidence; red records actual
  command, collected/failed counts, exit code and the reviewer's failure_kind. These are
  agent judgments backed by observed output, never a script's semantic classification.
  Missing/invalid envelopes are incomplete evidence, never approval. The wire format
  exists so the independent implementation issues and offline executor adapters agree;
  it does not constrain internal storage, helper names or the agents' way of reasoning.
- The preparation command alone appends attempt/review facts and promotes reviewed
  bytes into the plan. Agents return facts to that owner. Planning alone edits behaviour;
  publication alone publishes; the existing scheduler alone writes Approval snapshots.
  Status/progress are projections. A successor owner acts only after the old owner is
  known stopped, and concurrent prepare calls cannot promote two different results.
- Preserve completed candidate bytes for review resume and never promote partial files.
  An unchanged reviewed input reuses its result. Bind identity, statements and relevant
  shared context/references, commands, test/helper bytes, base revision and executor
  setup. Check freshness again after publication refresh. Changes require new evidence.
  Keep holds durable across interruptions; an unchanged QUESTION does not launch another
  attempt. Planning's clarified statement starts a new cycle with history retained.
- Put helpers needed by the generated checks inside configured acceptance_dir so today's
  acceptance_files publication and hashes lock them too. No new arbitrary helper roots,
  modified shared conftest/config, overwrites of another issue's files or symlink escapes.

## Issue ownership and rollout

All issues are s/m and assigned to order: order.py, commands/order.py and their local
preparation prompts belong to order under this task's ownership direction. No run_*.py
or scheduler/orch prompt issue is needed. Issue dependencies reflect actual interfaces:
statements -> writer -> review -> lock; resume consumes review's durable records. Work
in the same lane serializes naturally. The first issue fails closed for real publication
while later issues supply authoring/review, so no partially rolled-out statement issue
reaches implementation without locked checks. No existing published order is rewritten.

The skill update in this planning change states the new outcome for steps 4–6 while
preserving the locked fa-order wording. This introducing order deliberately uses the old
acceptance_files plus preread route; it cannot depend on a command it has not built yet.

## Verification and break-it bar

Acceptance tests drive CLI output, plans, scratch/publication effects and public Executor
adapters. They do not import new private production helpers. The fixtures use local bare
Git histories, emulate publication commit/push, fake both executors and never call GitHub
or send signals. Test runs are targeted, serial, and gated on uptime's 1-minute load < 8.

| Issue | Wrong value | Skipped effect | Doubled effect |
| --- | --- | --- | --- |
| statements | invalid/valid controls and issue-specific prepare error | dry-run must accept statements; publication must refuse | no publication/agent calls for validation |
| writer | exact lane model/context and actionable question/path | one writer invocation and isolated candidate path | writer count exactly one; main unchanged |
| review | exact reviewed files/commands and question/hold outcomes | reviewer session required; coverage/red evidence present | roles writer/reviewer once on pass; no third hold attempt |
| lock | exact published bytes/blob and stale-content refusal | tests reach published files and approved child; legacy preread still required | one child, one publication commit and one push |
| resume | persisted phase/reason/time and holds | review resumes with saved tests; partial writes never promoted | unchanged prepare/status does not repeat work; writer not rerun for review wait |

The lock issue also runs the existing targeted merge-policy guard. This planning run
checks the existing preread and acceptance-baseline guards separately. Keep those existing
acceptance paths out of a new issue's Done when ownership: adding them there would make
them look owned by pending work. The existing runtime baseline must continue protecting
those previously passing guards; this order does not reassign or republish their files.
Failure of a new test on current main is expected; a collection/setup/import failure is
not acceptable evidence. Run results and limits are reported separately, not fabricated
as passing implementation checks. Candidate test break-it probes include correct, wrong
value, skipped receipt and doubled receipt cases through the toy project's public CLI.

## Perspective answers

All 83 supplied questions from all seven seats are accounted for below. Many asked about
the earlier after-publication design and dissolve under decision 2. No timing/cost or
incident-frequency claim is made. The only added WHATs close concrete gaps in state
ownership, provenance/freshness, bounded recovery and truthful status (plus the necessary
public preparation/schema boundary). Details answered by current code remain in its
existing publication, Approval, D11 and baseline path.

### Answer 1: Before publication, not a gate phase

Sources: Decisions 2, 4, 8; order.py: publish/preread.

Test writing and review run in local order preparation before any statement issue is published. The existing publication commits one batch of spec and tests. No new runtime gate phase, premature approved issue, postapproval body mutation or per-issue test commit is introduced. This order itself uses old-style failing locked tests, never done_when: true.

Question IDs: `0ea3e9df04c1-skeptic-1`, `0ea3e9df04c1-skeptic-6`, `0ea3e9df04c1-skeptic-8`, `0ea3e9df04c1-skeptic-12`, `0ea3e9df04c1-first_time_user-2`, `0ea3e9df04c1-first_time_user-9`, `0ea3e9df04c1-first_time_user-11`, `0ea3e9df04c1-first_time_user-14`, `0ea3e9df04c1-operator-1`, `0ea3e9df04c1-operator-7`, `0ea3e9df04c1-maintainer-2`, `0ea3e9df04c1-maintainer-3`, `0ea3e9df04c1-maintainer-11`, `0ea3e9df04c1-incidents-5`, `0ea3e9df04c1-incidents-6`, `0ea3e9df04c1-incidents-7`, `0ea3e9df04c1-incidents-13`.

### Answer 2: What planning and the lane own

Sources: Decisions 1, 5; D98; order.py: preread_prompt/preread_target.

Planning supplies plain observable statements, scope, contracts and dependencies. Use an explicit statements list so new issues cannot accidentally switch to the legacy route after files appear. Lanes own HOW, write through public project entry points and receive the full shared spec. Review checks cross-issue contracts as well as per-statement coverage. Existing locked tests are read as constraints, and a real conflict returns QUESTION before lock.

Question IDs: `0ea3e9df04c1-skeptic-3`, `0ea3e9df04c1-skeptic-7`, `0ea3e9df04c1-first_time_user-1`, `0ea3e9df04c1-first_time_user-6`, `0ea3e9df04c1-maintainer-1`, `0ea3e9df04c1-maintainer-7`, `0ea3e9df04c1-incidents-12`.

### Answer 3: Independent reviewer and break-it

Sources: Decision 3; skills/fa-order/SKILL.md step 5; config.py: resolved_reviewer; run_implementation.py: lane_config.

Use the configured reviewer resolution for the issue lane and a different session, with the break-it rule and risk map in context. A configured same executor is allowed but a shared session is not. A missing reviewer waits; it does not approve itself or silently fall back. Writing and implementation are separate runs, and implementation cannot lower a locked bar.

Question IDs: `0ea3e9df04c1-skeptic-5`, `0ea3e9df04c1-first_time_user-3`, `0ea3e9df04c1-first_time_user-8`, `0ea3e9df04c1-first_time_user-13`, `0ea3e9df04c1-incidents-4`, `0ea3e9df04c1-other_users-1`, `0ea3e9df04c1-other_users-8`.

### Answer 4: An assertion for the right behaviour

Sources: Decisions 2, 3, 5; .foragent-risks.yml; D65.

Keep actual current-main revision, commands, results and reviewer coverage evidence. Tests must collect and fail by a behavioural assertion. Import/syntax/collection failures, zero tests, all skips/xfails and unconditional failure are not proof. Agents judge the cause and break-it quality, scripts record facts. Already-working behaviour, ambiguous behaviour or an unavailable harness returns QUESTION; no fake red test. Non-machine-checkable requirements get a checklist plus a runnable check after clarification.

Question IDs: `0ea3e9df04c1-skeptic-9`, `0ea3e9df04c1-first_time_user-4`, `0ea3e9df04c1-first_time_user-16`, `0ea3e9df04c1-attacker-3`, `0ea3e9df04c1-operator-5`, `0ea3e9df04c1-maintainer-5`, `0ea3e9df04c1-incidents-3`, `0ea3e9df04c1-other_users-3`, `0ea3e9df04c1-other_users-4`.

### Answer 5: Durable owner, bounded holds and resume

Sources: Decision 3; D95; actual gap: review and resume records do not exist.

The preparation command alone appends attempt/review facts and promotes candidates; agents only return results. Preserve per-issue holds across CLI restarts. Second quality hold becomes QUESTION to planning; interruption/quota is not a hold. Retain completed candidate bytes for review resume, never promote partial writes, reuse an unchanged reviewed result and take over only from a known stopped owner. Status reads that same evidence, with waiting, interruption, writing, reviewing and QUESTION separated. Planning handles unanswered questions even without an orchestrator or screen.

Question IDs: `0ea3e9df04c1-first_time_user-5`, `0ea3e9df04c1-first_time_user-12`, `0ea3e9df04c1-operator-2`, `0ea3e9df04c1-operator-3`, `0ea3e9df04c1-operator-4`, `0ea3e9df04c1-operator-11`, `0ea3e9df04c1-maintainer-4`, `0ea3e9df04c1-maintainer-6`, `0ea3e9df04c1-incidents-8`, `0ea3e9df04c1-incidents-9`, `0ea3e9df04c1-other_users-6`.

### Answer 6: Exact reviewed content and current main

Sources: Decisions 3, 4; D7; order.py: acceptance_blobs; scheduler_approval.py: _acceptance_matches.

Tie evidence to order/issue identity, statements and shared contract/reference context, commands, test/helper bytes, base revision and resolved executor setup. Recheck validity after base refresh under the publish lock. Changed content or main requires preparation evidence again, never a legacy fallback. Existing Approval/body/blob invalidation still applies after lock; conflicts go through a clarified/reapproved order edit rather than workers changing locked checks.

Question IDs: `0ea3e9df04c1-first_time_user-7`, `0ea3e9df04c1-attacker-4`, `0ea3e9df04c1-attacker-5`, `0ea3e9df04c1-operator-6`, `0ea3e9df04c1-operator-8`, `0ea3e9df04c1-maintainer-10`, `0ea3e9df04c1-incidents-1`, `0ea3e9df04c1-incidents-10`.

### Answer 7: Confinement, helpers and process safety

Sources: Decision 6; .foragent-risks.yml; order_files.py: check_destinations/check_write; D52.

Only new issue-owned tests/helpers inside acceptance_dir may be promoted. Reject source, shared collection/config edits, conflicting paths and symlink escapes; keep original checkout and remote/approval/credential writes unavailable. Run checks in confined scratch against unchanged main source. Pass existing locked checks and risk guidance to both agents. Process-control checks use fake tables only. Treat statements/test comments as task data, never authority to alter review or publication rules; existing issue formatting validation still applies.

Question IDs: `0ea3e9df04c1-attacker-1`, `0ea3e9df04c1-attacker-2`, `0ea3e9df04c1-attacker-6`, `0ea3e9df04c1-attacker-7`, `0ea3e9df04c1-attacker-8`, `0ea3e9df04c1-maintainer-8`, `0ea3e9df04c1-incidents-2`, `0ea3e9df04c1-incidents-11`.

### Answer 8: Legacy and baseline protection

Sources: Decision 1, 4; order.py: require_clean_preread; run_baseline.py; policy.py.

A present statements field selects reviewed preparation; otherwise existing acceptance_files and clean preread retain the old route. Mixed orders keep each issue's route, files and hashes. No migration or deletion deadline for published legacy plans is part of this order. Prepared files use the existing issue ownership and baseline path; no new empty-baseline exemption. Preparation does not rewrite approval snapshots or existing pending/running issues.

Question IDs: `0ea3e9df04c1-skeptic-11`, `0ea3e9df04c1-first_time_user-10`, `0ea3e9df04c1-operator-10`, `0ea3e9df04c1-maintainer-9`, `0ea3e9df04c1-incidents-14`, `0ea3e9df04c1-other_users-9`.

### Answer 9: The person and the approval boundary

Sources: Decisions 3, 5, 7; D7/D11.

The interview already decided that the person approves behaviour/scope once and does not review tests before lock. Reviewer pass precedes that approval, so this is not a second human-review preference or init setting. The person is asked only about unresolved WHAT. Recovery through clarified, reapproved order edits remains available; this order does not replace the decided reviewer check with after-the-fact detection.

Question IDs: `0ea3e9df04c1-skeptic-4`, `0ea3e9df04c1-skeptic-13`, `0ea3e9df04c1-other_users-7`.

### Answer 10: Budget, costs and evidence limits

Sources: Decisions 2, 4; D65/D93/D96; existing preread admission contract.

This is an ownership/context correction, not a measured throughput claim. No evidence establishes minutes saved, incident counts or extra quota consumption; do not invent any. Preparation replaces the prior read, uses existing admission/accounting rather than a runtime gate cycle, and records waits. No omit-tests/cost-policy setting was requested. Observe real session usage before proposing resource-policy changes.

Question IDs: `0ea3e9df04c1-skeptic-2`, `0ea3e9df04c1-skeptic-10`, `0ea3e9df04c1-operator-9`, `0ea3e9df04c1-other_users-2`.

### Answer 11: Ownership and language

Sources: .foragent.yml; order.py: preread_target; Decision 1.

All five issues change order preparation/commands and their prompts, owned here by order. No run_*.py or orch scheduler changes are needed. Planning talks in the configured language; repository statements/tests/specs use English by the existing skill policy, while preserving original supplied text and meaning in review. Other languages and project test tooling remain valid inputs, without a semantic keyword classifier.

Question IDs: `0ea3e9df04c1-first_time_user-15`, `0ea3e9df04c1-other_users-5`.

### Answer 12: This order proves itself

Sources: Decision 4; this plan acceptance_files and done_when.

Use the current schema and old-style acceptance_files for this bootstrap order, with public CLI/file checks and targeted regressions. Every feature issue has executable assertions, valid controls, a sharp negative case and a break-it rationale. No issue can close on true; D11/baseline regression checks remain in the lock issue's commands.

Question IDs: `0ea3e9df04c1-other_users-10`.

## Out of scope

Rewriting, migrating or replacing existing published plans; a runtime gate test-authoring
phase; changing D11, Approval ownership or baseline/merge policy; a new init preference,
a model-selection or budget-policy redesign, guaranteed speedups, real signal tests,
new GUI/TUI implementation phases, and implementation of these feature issues in this
planning run. This run does not publish, commit, push or call GitHub.

## Assumptions

The smallest unambiguous public selector is an optional statements list, and the new
step is named `fa order prepare` with read-only `--status`. Helpers stay under the
configured acceptance directory to reuse the existing locking mechanism. Status for
this scope is exposed by that order command; a global lane-view redesign is not needed.
The supplied interview is the source of D98 and the two-hold rule; this checkout's
D95 text independently supplies one state owner and append-only facts (it does not
itself spell out the two-hold limit). No claim that absent D98 text exists in decisions.md.

## Verification performed in this planning run

- red: 39 tests, 39 failures, 0 errors, 0 skips; exit 1. Immediately preceding uptime: 1:27  13 users, load averages: 6.48 8.04 8.74.
- regression: 47 tests, 0 failures, 0 errors, 0 skips; exit 0. Immediately preceding uptime: 1:24  13 users, load averages: 7.97 9.21 9.23.

All 39 new failures were assertions: 28 expose the missing prepare command, 10 expose the missing statement/schema boundary, and one reproduces actual approval with empty acceptance_files after clean preread. They are expected red checks for future implementation, not passing feature tests. The 47 regressions include all seven test_fa_order_* files, existing preread, acceptance-baseline and test_merge_decision checks. Both pytest runs were targeted and serial, with plugin autoload disabled and temporary directories confined to this workspace.

Additional checks: current validate_plan schema passed; the public publish --dry-run --approve CLI passed with an offline FakeTracker and workspace-local state; skill-creator quick_validate passed. The candidate's public CLI probe passed the correct implementation and failed by assertion for wrong value, skipped receipt and doubled receipt. No production source was changed, no prohibited Git command was executed and no GitHub call was made.
