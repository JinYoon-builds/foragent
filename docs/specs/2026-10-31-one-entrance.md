# One entrance: the PM session

Jin's decisions of 2026-10-31 are the source of this order. This is an offline,
unpublished plan with locked acceptance drafts; it does not implement the feature.
The person talks to one PM. FA owns durable work; terminal sessions are replaceable.

## Root cause and reuse

Today's CLI requires a subcommand, so bare fa is not an entrance. PM-like presence
is inferred from absence of a role environment variable, and the planning heartbeat
contains only a time, not the caller who holds authority. That gives every eligible
session the same rights. A statusline can renew that heartbeat without an explicit
seat claim. Fix authority at admission, not just one reported stale action.

Interview progress already persists in FA, including ledger/chunks/cells/questions.
The gap is recovery and PM context, not a missing interview engine. Questions and
action notes already have durable FA records. The current no-PM question path sends
a direct notification; the new decision keeps it for the PM. The spawner currently
prefers Orca or tmux terminals. Existing role scaffolding, append-only pending
notebooks, work preservation, supervisor and run facts should be reused.

Runs currently receive their own lane memory, but not the other lanes' path map or
a shared append tool for another owner. The agent lacks context and capability.
Give it those; do not have scripts classify lesson meaning. Completed-order facts
currently omit crossing issues/lane pairs. Computing those from actual paths is
fact calculation, not a rule for redrawing boundaries.

## Scope and ownership

The nine supplied decisions are retained. Public screens, status, current docs,
skills and descriptions use PM. PM seats are scoped to person and repository.
A takeover denies new publication/approval by the old session without killing it
or revoking already accepted FA work. Its remaining notes may still be kept in FA.
Closing any session loses no accepted work or unfinished interview.

The PM definition and notebook are project files. Personal language/tone remains
in personal memory. PM lessons use the existing append-and-curate path, with a
single pending append owner and a single curator. Notes about decisions stop at
being kept in FA; who promotes them to the decision document remains open.

All listed implementation files match their issue's existing .foragent.yml lane.
No new lane is invented and no tests/ or src/ file is edited by this draft.
Where broad orch globs overlap the newer roles lane, use the specific roles owner.
Acceptance publication paths are locked-test outputs, not lane implementation files.
Implementation names and private storage shapes remain the lane's choice.

### Ownership gaps that this draft cannot honestly assign

The configured paths omit README.md, current docs/**, skills/fa/**,
skills/fa-design-session/**, src/foragent/commands/**,
src/foragent/messages/**, src/foragent/presence.py,
src/foragent/brief*.py, src/foragent/role_agents.py and src/foragent/lane_memory.py.
Decision 1's full copy/guidance migration and parts of decisions 4, 7 and 8 need
these sources. Feature issues are assigned to the owned entry-point files, but
that is not an ownership claim over these helpers. Before implementation changes
an omitted path, the repository's lane map needs an explicit owner for it.
The whole-source copy gate intentionally includes current unowned guidance;
it is not narrowed just to make the existing lane map appear complete.
No extra blocked_by key or fake issue is manufactured for an ownership decision.

### External work

The hidden-orchestrator issue starts only after the github-writes order
(init-fa-allow) is merged. That already drafted order makes GitHub writes use FA
commands and init proposes Bash(fa:*), avoiding invisible permission prompts.
It is unpublished, so its keys/numbers are absent here and are not fabricated in
blocked_by. An empty internal blocked_by list does not clear this prerequisite.

Decision notes reuse #1043's D85 batching, including append-only waiting notes
and a flush-owned checkpoint. That public note/pending control is absent from
this main snapshot. Reconcile that existing external work before implementing
decision-notes; do not replace or duplicate it.

## Contracts

These are public/inter-issue behaviour contracts, not private implementation names.

- An explicit bare fa entrance uses the existing session identity boundary
  (FORAGENT_SESSION_ID; session_id in statusline hook payloads) to acquire the PM
  seat. Dependent features can read the current holder and validate their caller.
  The seat issue defines the code interface in its merged skeleton; names belong
  to its lane. Hooks/heartbeats are readers/renewers, not new seat claims.
- Existing fa order progress remains the interview writer. fa order context --json
  adds interview_progress, equal to the complete persisted progress or null.
  The next PM's startup/prompt context briefs it without resetting it.
- fa order preread PLAN --background --approve-if-clean --json submits immutable,
  authorized work and returns without running the agent in the PM shell.
  Pending work remains visible in order context after the plan file disappears.
  FA's gate can execute it with the configured executor independently of PM.
  Results and approval evidence reach the issue; a QUESTION stays unapproved and
  enters the durable question box. A repeated gate turn does not redo completion.
- The existing #1043 fa order note --defer BOUNDARY --kind decision --text TEXT
  and fa order pending --json are reused. No new promotion policy is implied.
- Existing fa orch ask/questions/answer and action-note records remain tool
  boundaries. The active PM's statusline --prompt-context receives their facts,
  including action/reason/next and question/options/recommendation. With no PM,
  person messages wait in FA rather than being sent directly.
- fa orch stop --all --keep-work --why TEXT is the bulk maintenance boundary.
  fa stop exposes the same effect when no PM exists: all lanes hold, running work
  is saved before the run stops, and no fresh run starts until a later resume.
  The view entrance adds no second run-state writer.
- fa init creates .claude/agents/fa-pm.md and an empty
  .claude/agent-memory/fa-pm/MEMORY.md, preserving user-owned contents on re-init.
  Pending PM lessons use MEMORY.new.md and the existing curator path.
- fa lanes lesson --lane OWNER --issue SOURCE --id DELIVERY --text TEXT
  --repo-root ROOT appends the agent-selected lesson to that owner's pending
  notebook with source issue and owner. One delivery id is idempotent. It never
  rewrites curated memory. This is an FA append capability, not a prose classifier.
  Runs receive the lane map and tool context; the agent chooses whether to use it.
- fa orch facts --json adds cross_lane_orders for completed orders. Each public row
  is {order: ISSUE, issues: [{number: ISSUE, lane: SOURCE, other_lanes: [OWNER]}],
  lane_pairs: [[LANE, LANE]]}. Only actual changed paths count; omit same-lane work,
  deduplicate pairs and execution retries, and use stable sorting. The issue/order
  records and run history are authoritative; no stored aggregate is introduced.
- The lane screen shows only the short current-work line for the orchestrator.
  Detailed inspection is explained by the PM from the records. Maintainer tools
  such as fa orch watch remain available, outside person guidance.

## Source boundaries and assumptions

The literal fa CLI entrance is used, with the same seat applying to its PM
session; adding a parallel entrance for slash commands is not a separate product
decision in this order. Tests inject existing session ids and fake external
adapters rather than pinning a new private session implementation.

The copy check covers README, current docs, all shipped skill markdown and agent
descriptions, and non-docstring Python string literals across the shipped package.
Read-only identifiers, comments and docstrings are exempt. decisions.md and dated
spec/research/archive documents retain historical wording under D108; they are not
current operating guidance. Current ideas.md and rules.md remain in the check.

The new public flags/commands above are minimal inter-issue test contracts. They
do not choose module/function names, storage formats, or the agent's sequence.
No new style skill, library, automatic judgement threshold or message channel is
introduced. The tests explicitly supply the agent's choices and preread answers.

## Issue map

| key | lane | size | blocked_by |
| --- | --- | --- | --- |
| pm-seat | view | m | — |
| interview-state | order | m | — |
| decision-notes | order | s | pm-seat |
| pm-job-queue | order | m | pm-seat |
| fa-job-worker | orch | m | pm-job-queue |
| pm-messages | roles | m | pm-seat |
| hidden-orchestrator | orch | m | — |
| stop-runtime | orch | m | — |
| fa-stop | view | s | stop-runtime |
| pm-init | board | s | — |
| cross-lane-lessons | skills | m | — |
| lesson-context | orch | s | cross-lane-lessons |
| lane-pair-facts | roles | m | — |
| pm-context | view | m | pm-seat, interview-state, pm-messages, pm-init |

Dependencies are code/behaviour prerequisites, not a scheduling queue:
the gate worker consumes the new durable queue; the single stop entrance consumes
bulk stop; run lesson context depends on its append tool; PM context consumes the
seat and persisted sources. Notes/messages need PM authority. Hidden launch and
lane-pair facts are independent internal work, subject to the external prerequisite
above. No blocked_by references an unpublished external key or numeric issue.

## Acceptance and the break-it rule

All checks are public CLI, public runner/gate entry points or file boundaries.
Existing fake tracker/Git/executor fixtures are reused. No implementation helper is
imported as a behavioural oracle. The shared seat test module also provides test
setup to other locked files; publication ships all those files together.

A skipped effect fails on a missing seat/context/record/notebook/branch or missing
lesson. A wrong owner or result fails on stale publication, exact question context,
actual saved bytes, or an incorrect crossing row. Duplicate effects fail on issue,
model-call and lesson counts. Same-lane work is a negative facts case; QUESTION is
a negative approval case; two terminal hosts exercise hidden launch. Stop result
assertions live outside the fake executor so runner error handling cannot swallow
the assertion and accidentally pass through generic WIP preservation.

Only targeted pytest invocations are run, without xdist. Each Done when starts
with uv run pytest. These acceptance files fail intentionally on the recorded main
revision; they are to be published/locked before implementation, not changed by it.

## Offline baseline evidence

The worktree began at main/HEAD 138ea66975085f4f5d1ce66fbf97f1462970ee66. The shared main ref advanced externally while this draft was prepared. No checkout/reset/commit/push/stash was used. Final verification used a git archive of main f8e47fb157b31c02ee5be5ddef89cff35bf58894 with byte-identical staged acceptance copies inside that snapshot, all under the given working directory. The source checkout and its tests were left untouched.

Each file was run separately below with uv run pytest -q, without xdist. UV_OFFLINE=1 and UV_NO_SYNC=1 used the existing dev environment. PYTHONPATH selected the archived main source and existing test helpers; PYTEST_PLUGINS=conftest loaded the repository isolation, signal and Git-command guards. All HOME/XDG, pytest basetemp, TMPDIR and uv cache writes were confined to disposable order-out/.run.

### test_one_entrance_copy.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_copy.py`; exit 1.

```text
E       AssertionError: Current person-facing text still uses the retired PM name:
E       AssertionError: Explain the single emergency entrance and the PM
FAILED order-out/tests/acceptance/test_one_entrance_copy.py::test_current_person_facing_sources_use_pm_everywhere
FAILED order-out/tests/acceptance/test_one_entrance_copy.py::test_person_guidance_routes_inspection_and_stop_through_pm
2 failed in 1.30s
```

### test_one_entrance_facts.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_facts.py`; exit 1.

```text
E           AssertionError: Architect facts must include completed orders' cross-lane evidence
FAILED order-out/tests/acceptance/test_one_entrance_facts.py::test_finished_order_facts_name_crossing_issues_and_lane_pairs_without_double_counting
1 failed in 0.73s
```

### test_one_entrance_hidden.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_hidden.py`; exit 1.

```text
FAILED order-out/tests/acceptance/test_one_entrance_hidden.py::test_orchestrator_launch_never_opens_a_person_terminal[orca]
FAILED order-out/tests/acceptance/test_one_entrance_hidden.py::test_orchestrator_launch_never_opens_a_person_terminal[tmux]
2 failed in 0.70s
```

### test_one_entrance_init.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_init.py`; exit 1.

```text
E       AssertionError: fa init must create .claude/agents/fa-pm.md
FAILED order-out/tests/acceptance/test_one_entrance_init.py::test_new_project_has_pm_definition_and_empty_project_notebook
1 failed in 1.10s
```

### test_one_entrance_interview.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_interview.py`; exit 1.

```text
E       AssertionError: FA context must carry the complete persisted interview
E           AssertionError: The next PM lacks persisted interview fact: Resume customer intake
FAILED order-out/tests/acceptance/test_one_entrance_interview.py::test_order_context_exposes_the_persisted_interview_for_the_pm
FAILED order-out/tests/acceptance/test_one_entrance_interview.py::test_reopening_resumes_ledger_questions_chunks_and_cells
2 failed in 0.97s
```

### test_one_entrance_jobs.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_jobs.py`; exit 1.

```text
E       AssertionError: FA must accept durable preread/approval work: usage: fa [-h] [--version]
E       AssertionError: FA must accept durable preread/approval work: usage: fa [-h] [--version]
E       AssertionError: FA must accept durable preread/approval work: usage: fa [-h] [--version]
FAILED order-out/tests/acceptance/test_one_entrance_jobs.py::test_enqueue_returns_before_the_agent_and_keeps_the_plan_after_the_shell_closes
FAILED order-out/tests/acceptance/test_one_entrance_jobs.py::test_gate_finishes_preread_approval_once_and_records_evidence_on_the_issue
FAILED order-out/tests/acceptance/test_one_entrance_jobs.py::test_a_question_does_not_turn_into_approval_after_the_pm_closes
3 failed in 0.78s
```

### test_one_entrance_lesson_context.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_lesson_context.py`; exit 1.

```text
E       AssertionError: The agent cannot identify another lane's area without the lane map
FAILED order-out/tests/acceptance/test_one_entrance_lesson_context.py::test_run_can_see_other_lane_paths_and_the_fa_lesson_tool
1 failed in 2.35s
```

### test_one_entrance_lessons.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_lessons.py`; exit 1.

```text
E       AssertionError: The run needs an FA append tool for the owning lane: usage: fa lanes [-h] {add,skills,talk,dreamed} ...
FAILED order-out/tests/acceptance/test_one_entrance_lessons.py::test_cross_lane_run_appends_the_selected_lesson_once_with_source
1 failed in 2.86s
```

### test_one_entrance_messages.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_messages.py`; exit 1.

```text
E       AssertionError: A hidden orchestrator must leave person messages for the PM, not push them directly
FAILED order-out/tests/acceptance/test_one_entrance_messages.py::test_question_waits_in_fa_when_no_pm_exists_without_direct_delivery
1 failed in 0.73s
```

### test_one_entrance_notes.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_notes.py`; exit 1.

```text
E           AssertionError: PM decision notes must use the durable D85 append route: usage: fa order [-h]
FAILED order-out/tests/acceptance/test_one_entrance_notes.py::test_decision_notes_survive_the_pm_without_editing_the_decision_document
1 failed in 0.68s
```

### test_one_entrance_relay.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_relay.py`; exit 1.

```text
E           AssertionError: The PM lacks the orchestrator's question context: q1
E       AssertionError: The next PM must inherit the repo's PM lessons
E           AssertionError: The PM needs the actual records to explain the orchestrator: Reviewing issue 41
E       AssertionError: assert ('An internal...ostic detail' not in '● resting ·...e · q quit\n'
FAILED order-out/tests/acceptance/test_one_entrance_relay.py::test_orchestrator_question_reaches_only_the_current_pm_and_survives_reopening
FAILED order-out/tests/acceptance/test_one_entrance_relay.py::test_project_pm_notebook_reaches_the_next_teammate_and_retains_pending_lessons
FAILED order-out/tests/acceptance/test_one_entrance_relay.py::test_pm_inspection_context_includes_the_recorded_reason_and_next_step
FAILED order-out/tests/acceptance/test_one_entrance_relay.py::test_lane_screen_shows_one_line_of_the_orchestrators_current_work
4 failed in 0.94s
```

### test_one_entrance_seat.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_seat.py`; exit 1.

```text
E       AssertionError: Bare fa must acquire the PM seat in this terminal
E           AssertionError: Each explicit fa entrance must take the PM seat
FAILED order-out/tests/acceptance/test_one_entrance_seat.py::test_second_fa_takes_the_seat_and_old_pm_cannot_publish_or_approve
FAILED order-out/tests/acceptance/test_one_entrance_seat.py::test_a_third_terminal_can_take_over_without_killing_the_old_terminal
2 failed in 0.74s
```

### test_one_entrance_stop.py

Command: `uv run pytest -q order-out/tests/acceptance/test_one_entrance_stop.py`; exit 1.

```text
E       AssertionError: One stop must reach FA's save-and-stop path: usage: fa orch stop [-h] [--repo-root REPO_ROOT] --keep-work --why WHY number
E       AssertionError: One stop must reach FA's save-and-stop path: usage: fa [-h] [--version]
FAILED order-out/tests/acceptance/test_one_entrance_stop.py::test_stop_keeps_running_work_and_leaves_all_lanes_stopped[bulk]
FAILED order-out/tests/acceptance/test_one_entrance_stop.py::test_stop_keeps_running_work_and_leaves_all_lanes_stopped[entrance]
2 failed in 4.99s
```

Final result: 13 files, 23 failed tests, zero collection/setup errors. All failures identify missing product behaviour. The stop tests assert the command result outside the fake executor, so generic error/WIP preservation cannot make a missing stop feature pass.

## Publish dry-run evidence

`fa order publish --dry-run order-out/plan.json` exited 0 with `Plan (dry run): One entrance: the PM session`, listing all 14 issues in the configured lanes and the 13 acceptance files plus the eventual parent spec. For the network-blocked run, a strict local gh adapter returned an empty issue list for only the repository issue-list read and rejected every other request. HOME/XDG were isolated under the working directory. This validates schema, embedded files, lane names and plan-key dependencies against an offline tracker; it does not reconcile live issues or satisfy external merge/ownership prerequisites. No actual publication occurred.
