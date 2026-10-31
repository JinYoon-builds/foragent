# D99: roles and dreams wake when an order finishes

This order edits only #997 (architect-schedule) and #999 (orchestrator-retrospective).
It replaces the corresponding sections of the 2026-10-30 load order; the remaining
sections retain their existing scope. D99 is the current wake decision.
Tests keep their historical function names and parameter ids.

## 7. Order-finish roles and architect reviews (architect-schedule)

Root cause: architect only launches on orchestrator calls; role signals notify
rather than waking it. Held-execution counts miss many rounds in one run. It
lacked a wake instruction/tool connection. Proposals/r28 remain agent judgments.
Reuse the existing scheduling and role execution paths for order-finish wakes,
shared with section 9. Reuse role execution/liveness, quota/hours,
remaining slots and low OS priority where supported. Ready issue/dream work
gets slots first. No daemon/cron/new timer. --no-start forbids ALL automatic
role/wake launches, including finalizer paths.

One top-level boolean `scheduled_roles: true`, default on, controls BOTH automatic
architect and retrospective. Validate/load/serialize via normal settings and
committed tick config; document in docs/gate.md section 9 (settings reference).
False explicitly holds/drains these project roles. Existing machine-readable
project hold/drain and any live orch_plan pause suppress dispatch AND delivery.
This export has no separate project-drain record: when intent is only prose,
the orchestrator must set this switch or plan pause, never classify the prose
or create another hold store. Conservatively any active lane pause defers these
whole-project roles; expiry permits eligibility again. Manual role commands
retain existing explicit-command semantics.

Architect needs new stored merge/run input since last successful covered_until.
D99 (Jin, 2026-10-31): architect review, the docs role, dreams of the lanes
touched by an order and orchestrator retrospective become due when every child
issue of that published order is merged or closed. An unfinished order stays
unfinished when lanes are idle, a lane queue empties, a day passes or ten merges
land. A finished order remains eligible while unrelated orders are still busy.
No fixed merge-count or empty-queue dream fallback remains. Without new merge
or run input no architect review runs, even for a newly finished order. Ignore
role calls/completions/heartbeats/signals as input. Session alone appends done;
success covers INPUT START cutoff so arrivals during review remain due. No
second last-review writer or fixed failure-count trigger.

Failed launch/quota/execution stays due but retries no sooner than one elapsed
UTC hour after failure, or an existing executor reset/pause if later. Share
that retry floor with retrospective delivery/open failures and unacknowledged
pending wakes. Keep one pending request, no per-tick new request/window. Capacity
deferral never advances completion. Derive last attempt/error/next retry from
task events. UTC backoff deadlines include sleep; wake still obeys guards
and starts no catch-up burst. This differs from active-time acquisition waits.

Expose localized schedule facts in fa orch facts: last success/input cutoff,
due/reason, suppression, last attempt/error, next retry and pending age. Missing
role or supported opener is visible unavailable/deferred. Headless/Codex-only
projects can see the pending task and record a marked note; add no backend.
Interactive orchestrator lifetime consumes no slots. Unsupported nice is visible
best effort; capacity/holds still apply.

Append D99, dated 2026-10-31, to docs/decisions.md in this issue: Jin's
order-finish wake decision supersedes the 2026-10-30 idle/daily decision and
D82 addendum 5-4 (5-④), "no fixed schedule calls". Keep the old decision
as history and proposal acceptance with orchestrator/person. Keep every existing
architect skill section/reference; add Project review, changed merges/run input,
cutoff, structure facts and spend versus size/scope drift versus declared Files.
Amend manual-only provenance. Proposals use fa orch request --from architect;
no mandatory investigation order or automated proposal acceptance.

## 9. Note-based retrospective (orchestrator-retrospective)

Root cause: write_note overwrites orch-note.json, destroying retrospective
completion evidence. Keep latest-note projection AND append exactly one kind=note
journal event per successful fa orch note, preserving now/why/next/where and time
verbatim. Add explicit `fa orch note --retrospective --now TEXT`; event marker
retrospective=true. Unmarked notes never count. Reject blank input before writes.
No separate retro command/event kind/last-retro file/completion store. Derive
last-retro from latest marked journal note; retention keeps latest completion
even when old notes trim. Preserve existing output/JSON and person-found path;
person_found already exists and must not be reimplemented.

Use section 7's SAME hook/switch/hold/pause/backoff. Under D99, retrospective
becomes due when an order finishes: all its child issues are merged or closed.
Elapsed time alone never makes it due. A finished order can wake retrospective
within 24 hours of the last retrospective, while unrelated orders remain open.
Keep one existing inbox request per pending finished order; deliver to live orchestrator
or supported existing opener, retain pending/error/age/retry if unavailable.
Request/open/ack is not completion: only marked note is. Completion resolves
that order; repeated ticks without another finished order do not post duplicates. No lifetime terminal machine slot.

Orchestrator appends chosen lessons to configured MEMORY.md and records the
marked note. Code records facts, never lessons or their meaning. Keep SKILL.md
<=150 lines and reference references/retrospective.md, preserving action notes,
proposals/role signals/notifications and --person-found. New memory prose follows
project language, retaining quoted evidence. Invent no historical missing events.


## Verification on main (planning evidence)

The given checkout was main `7a4e40a5c47a52db3f48a7d5f2ab0fd6ae7a0278` at the start of this run.
While verification ran, another process advanced main to `0c473e00e4f4cf59613abef9f45b268d9987ac8c`;
its only added path was an unrelated order spec. A scoped git diff confirmed
that src/, tests/, skills/, docs/gate.md and docs/decisions.md were unchanged.
The checkout's HEAD and production files were not changed by this run.

Only the five affected acceptance files ran, without xdist. Corrected copies
ran in a temporary overlay pointing to this checkout's unchanged main source,
docs, skills and test helpers. No original test or production file was edited.
The overlay was removed after verification.

Baseline: the original architect, lane-dream, retrospective and repeat-hold
files returned `13 failed, 10 passed in 4.70s`; the original team-v1 dream file
returned `2 failed in 0.86s`. Thus 15 failures predate this amendment.

Corrected copies returned `18 failed, 7 passed in 4.80s`. After sharpening the
retrospective wake to occur within 24 hours, that file was rerun and returned
`3 failed in 1.03s`; the other corrected copies were unchanged.
Final collection returned `25 tests collected in 0.74s`, exit 0, no collection
errors. AST checks confirmed all test function names, argument lists and
parameter decorators/ids remain identical. All corrected copies compile.

| Corrected file | Result on main |
|---|---|
| test_architect_project_schedule.py | 10 failed, all also failed before amendment |
| test_lane_dream.py | 2 failed, 4 passed; 2 new D99 failures |
| test_team_v1_dream.py | 2 failed, both also failed before amendment |
| test_orchestrator_retrospective.py | 3 failed, all also failed before amendment |
| test_orch_repeat_hold.py | 1 failed, 3 passed; 1 new D99 failure |

### Changed test names

`tests/acceptance/test_architect_project_schedule.py`:

- `test_idle_tick_starts_one_project_review_without_role_signals_and_no_new_work_does_not_repeat`
- `test_busy_project_reviews_new_records_at_daily_boundary_without_signals`
- `test_success_watermark_covers_session_start_so_records_arriving_during_review_remain_due`

`tests/acceptance/test_lane_dream.py`:

- `test_lane_dream_empty_queue_completion_persists_per_lane_until_a_new_merge`
- `test_lane_dream_backlogged_lane_is_due_at_ten_merges_since_its_last_dream`
- `test_lane_dream_gate_respects_capacity_and_starts_only_one_per_tick_without_usage`
- `test_lane_dream_worker_receives_memory_only_prompt_and_completion_records_person_line`

`tests/acceptance/test_orch_repeat_hold.py`:

- `test_a_daily_retrospective_finds_lost_run_time_and_repeated_failures`

`tests/acceptance/test_orchestrator_retrospective.py`:

- `test_daily_retrospective_wake_is_derived_from_record_and_not_status_note`
- `test_retrospective_skill_uses_record_and_orchestrator_memory_within_line_cap`

`tests/acceptance/test_team_v1_dream.py`:

- `test_only_designated_person_can_offer_lane_dream`
- `test_foreign_open_memory_split_blocks_dream_for_matching_files`

The shared review_case fixture now supplies a published order. The new
order_children helper supplies only existing public plan markers and native
tracker links. Other architect cases retain switch, capacity, cutoff, retry
and documentation checks. Dream ownership/overlap tests now supply a finished
order so their existing protections remain meaningful under D99.

### Actual failure lines

`tests/acceptance/test_architect_project_schedule.py`:

```text
tests/acceptance/test_architect_project_schedule.py:99: in test_idle_tick_starts_one_project_review_without_role_signals_and_no_new_work_does_not_repeat
    assert len(case.launches) == 1, "Finishing an order must wake a review without role signals"
E   AssertionError: Finishing an order must wake a review without role signals
tests/acceptance/test_architect_project_schedule.py:140: in test_busy_project_reviews_new_records_at_daily_boundary_without_signals
    assert len(case.launches) == 1, "A finished order must wake its review while unrelated work is busy"
E   AssertionError: A finished order must wake its review while unrelated work is busy
tests/acceptance/test_architect_project_schedule.py:146: in test_success_watermark_covers_session_start_so_records_arriving_during_review_remain_due
    assert len(case.launches) == 1
E   AssertionError: assert 0 == 1
tests/acceptance/test_architect_project_schedule.py:191: in test_due_review_uses_only_a_slot_left_after_ready_issues
    assert len(case.launches) == 1, "A full-budget deferral must leave the project review due"
E   AssertionError: A full-budget deferral must leave the project review due
tests/acceptance/test_architect_project_schedule.py:199: in test_project_review_instructions_keep_existing_sections_and_proposal_route
    assert "## Project review\n" in skill
E   AssertionError: assert '## Project review\n' in '---\nname: fa-architect\ndescription: Measure where a Foragent project hurts and propose structural changes (module s...- [Tech-debt scoring](references/tech-debt-scoring.md): the common (Impact + Risk) × (6 − Effort)\n  priority score.\n'
tests/acceptance/test_architect_project_schedule.py:209: in test_scheduled_roles_share_switch_plan_pause_and_no_start
    assert getattr(case.ctx.config, "scheduled_roles", None) is True, "Scheduled roles need one default-on switch"
E   AssertionError: Scheduled roles need one default-on switch
tests/acceptance/test_architect_project_schedule.py:209: in test_scheduled_roles_share_switch_plan_pause_and_no_start
    assert getattr(case.ctx.config, "scheduled_roles", None) is True, "Scheduled roles need one default-on switch"
E   AssertionError: Scheduled roles need one default-on switch
tests/acceptance/test_architect_project_schedule.py:209: in test_scheduled_roles_share_switch_plan_pause_and_no_start
    assert getattr(case.ctx.config, "scheduled_roles", None) is True, "Scheduled roles need one default-on switch"
E   AssertionError: Scheduled roles need one default-on switch
tests/acceptance/test_architect_project_schedule.py:233: in test_failed_scheduled_review_backs_off_for_an_hour_without_advancing_success
    assert len(case.launches) == 1
E   AssertionError: assert 0 == 1
tests/acceptance/test_architect_project_schedule.py:254: in test_schedule_switch_loads_from_settings_and_documents_decision
E   foragent.config.ConfigError: unknown configuration key: scheduled_roles
```

`tests/acceptance/test_lane_dream.py`:

```text
tests/acceptance/test_lane_dream.py:80: in test_lane_dream_empty_queue_completion_persists_per_lane_until_a_new_merge
    assert dream_candidates(case) == set(), "Empty lane queues cannot finish an order"
E   AssertionError: Empty lane queues cannot finish an order
tests/acceptance/test_lane_dream.py:111: in test_lane_dream_backlogged_lane_is_due_at_ten_merges_since_its_last_dream
    assert dream_candidates(case) == set(), "Ten merges cannot wake dreams before the order finishes"
E   AssertionError: Ten merges cannot wake dreams before the order finishes
```

`tests/acceptance/test_orch_repeat_hold.py`:

```text
tests/acceptance/test_orch_repeat_hold.py:34: in test_a_daily_retrospective_finds_lost_run_time_and_repeated_failures
    assert "when an order finishes" in text
E   assert 'when an order finishes' in "--- name: fa-orchestrator description: keep a project's claude orchestrator session running, respond to blocked work,...e in the lane that owns its files; done when: all existing tests still pass, behaviour unchanged. then `fa tidy done`."
```

`tests/acceptance/test_orchestrator_retrospective.py`:

```text
tests/acceptance/test_orchestrator_retrospective.py:25: in test_retrospectives_append_verbatim_and_status_notes_do_not_replace_history
    assert cli_exit(case.main, ["orch", "note", "--retrospective", "--now", lesson,
E   AssertionError: assert 2 == 0
tests/acceptance/test_orchestrator_retrospective.py:69: in test_daily_retrospective_wake_is_derived_from_record_and_not_status_note
    assert len(due) == 1, "A finished order wakes retrospective within 24 hours using the marked record, not status notes"
E   AssertionError: A finished order wakes retrospective within 24 hours using the marked record, not status notes
tests/acceptance/test_orchestrator_retrospective.py:100: in test_retrospective_skill_uses_record_and_orchestrator_memory_within_line_cap
    assert "fa orch note" in skill + detail and "--retrospective" in skill + detail
E   assert ('fa orch note' in ("---\nname: fa-orchestrator\ndescription: Keep a project's Claude orchestrator session running, respond to blocked wor...e in the lane that owns its files; Done when: all existing tests still pass, behaviour unchanged. Then `fa tidy done`." + '# Repeat holds and the daily retrospective\n\n## Why count by defect class\n\nA reason code (`retries-exceeded`, `rev...e` event in `orch-events.jsonl`. Read them in\nthe retrospective and the tidy cycle as evidence for structural work.\n') and '--retrospective' in ("---\nname: fa-orchestrator\ndescription: Keep a project's Claude orchestrator session running, respond to blocked wor...e in the lane that owns its files; Done when: all existing tests still pass, behaviour unchanged. Then `fa tidy done`." + '# Repeat holds and the daily retrospective\n\n## Why count by defect class\n\nA reason code (`retries-exceeded`, `rev...e` event in `orch-events.jsonl`. Read them in\nthe retrospective and the tidy cycle as evidence for structural work.\n'))
```

`tests/acceptance/test_team_v1_dream.py`:

```text
tests/acceptance/test_team_v1_dream.py:19: in test_only_designated_person_can_offer_lane_dream
    assert "orch" not in dream_due(case.store, case.config, case.tracker, case.clock[0])
E   AssertionError: assert 'orch' not in ['orch']
tests/acceptance/test_team_v1_dream.py:35: in test_foreign_open_memory_split_blocks_dream_for_matching_files
    assert "orch" not in dream_due(case.store, case.config, case.tracker, case.clock[0])
E   AssertionError: assert 'orch' not in ['orch']
```

The requested D99-only failures are not attainable on this main without
implementing or weakening existing locked checks: main also lacks the
scheduled_roles setting, --retrospective CLI support, the project-review skill
section, and team dream ownership/overlap behavior. Existing failures remain
visible; no skip or xfail hides them.

### Assumptions

D99 replaces the long-order empty-queue fallback as well as idle/daily/ten-merge
triggers. Each current issue Goal is preserved and followed by an explicit D99
override. All other current body fields are preserved exactly.

The network is blocked. Dry-run uses the supplied current issue JSON through a
local FakeTracker transport, with lane/size from the original load plan, and
state confined to the temporary verification directory. It validates the local
plan and previews both edits; it does not verify live GitHub dependencies or
approval state. No real publish is performed.

### Dry-run

`fa order publish --dry-run order-out/plan.json` returned exit 0 using the
offline transport above. Output began:

```text
Plan (dry run): D99: roles and dreams wake when an order finishes
```

Both #997 and #999 were previewed with approval disabled.


# Not-now role signals stay quiet until facts change

## Problem and measured root cause

Requests r68/r69 reported the same idle lanes and ready work while the binding constraint stayed cap, despite a not-now answer. The answer had already settled those facts for the orchestrator.

On main, the gate treats a done request as eligible again after the signal clears or its cooldown expires. It does not use the structured not-now answer to distinguish that decision from a called answer, and it does not compare the answered signal's facts with current facts. The facts view already exposes the binding constraint and each signal's numbers, but the signal observation currently omits the binding constraint. A timer cannot express whether the orchestrator has new evidence to consider.

Evidence: `src/foragent/orch_role_raise.py`, `src/foragent/orch_roles.py`, and the public facts view in `src/foragent/orch_facts.py`. These are findings, not a prescribed implementation. This is factual state comparison; role invocation and meaning remain the orchestrator's judgment.

## Required behavior

After a role signal is answered not-now, the same facts produce zero further requests across repeated gate ticks, even after the previous cooldown expires. A change in that signal's facts makes it eligible for exactly one new request on the next tick, including inside the previous cooldown. For the reported idle-lanes signal, independently changing the binding constraint, the idle lanes, or their ready work counts as changed facts. An unanswered request is still raised only once. If the new request is answered not-now, its considered facts also stay quiet.

A different signal continues normally, including a different signal on the same role. Merely observing the facts again, advancing the clock, or changing facts belonging only to another signal does not re-arm the answered signal. The suppression survives a gate restart with the same state store reopened.

The result is visible through `fa gate tick --no-start --json`, `fa orch signal-answer ID not-now --why TEXT`, and `fa orch inbox --json`. No role is called automatically and signals continue to reach the orchestrator.

## Scope and ownership

One issue: `signal-not-now-sticks`, lane `orch`, size `s`, unattended. The lane owns the role-signal area through `src/foragent/orch*.py` in `.foragent.yml`. Implementation structure, internal names, persistence representation, and per-issue tool/review/domain skills belong to the lane under D98.

This order replaces the previous timed re-raise expectation for not-now in `docs/specs/2026-10-27-role-signal-raise.md` and `tests/acceptance/test_role_signal_raise.py`. That older acceptance file explicitly expects not-now to re-raise after 12 hours; that expectation conflicts with the newly approved behavior. It is recorded here so the implementer does not preserve the obsolete not-now rule or weaken this new locked test. The older file is unchanged in this planning-only delivery.

Out of scope: changing called-answer behavior, signal detection thresholds, role judgment or invocation, notification routing, and general scheduling. This delivery contains a plan and locked test only; it implements none of the feature.

## Locked acceptance and break-it rule

The locked file is `tests/acceptance/test_role_signal_not_now.py`. It uses the existing offline gate fixture and facts fake. It does not call or patch the role-signal raiser, seed suppression records, or inspect a private persistence shape. All raises, not-now answers, and request counts are observed through the public CLI. The restart check reopens the store explicitly; the shared gate helper also opens a fresh store for each tick.

Four scenarios produce six pytest cases:

1. Same facts across several ticks, including beyond 12 hours: rejects a timer-only fix and a comparison that mistakes the observation timestamp for new facts.
2. Changed facts, independently parameterized for binding, idle lanes, and ready work: each must yield exactly one new request within five minutes, no duplicate while open, and renewed suppression after a second not-now. Rejects a permanent mute, cooldown-only eligibility, and met/unmet-only comparison.
3. A different signal on the same role raises exactly once while the original stays quiet: rejects a global/role-wide mute and comparing the entire unrelated facts snapshot.
4. The same store reopened before later ticks retains suppression: rejects suppression kept only on the previous store instance.

Done when: `uv run pytest -q tests/acceptance/test_role_signal_not_now.py` passes after implementation. Run this file only; no xdist or whole suite.

## Assumptions

- Changed facts are relevant evidence for the answered signal; observation timestamps and another signal's independent facts do not count.
- New relevant facts re-arm the signal on the next tick without waiting for the old cooldown. With unchanged facts, elapsed time alone never re-arms not-now.
- Called-answer behavior remains unchanged. The requested restart boundary is reopening the persisted store, as specified, rather than simulating an operating-system reboot.
- The staged file runs outside `tests/` using the existing tests directory on PYTHONPATH and its conftest plugin; publication embeds exactly the same file under the acceptance directory.

## Main baseline: actual red test evidence

HEAD and main were both `7a4e40a5c47a52db3f48a7d5f2ab0fd6ae7a0278`. Product source and existing tests were unchanged.

Executed with the existing local pytest environment, bytecode and pytest cache disabled, and HOME/TMPDIR/XDG directories inside `order-out/.runtime`:

```sh
PYTHONPATH="$PWD/src:$PWD/tests:$PWD/tests/acceptance" /Users/apple/Dev/foragent/.venv/bin/python -m pytest -q --tb=short -p conftest -p no:cacheprovider --basetemp="$PWD/order-out/.runtime/pytest" order-out/tests/acceptance/test_role_signal_not_now.py
```

Pytest exited 1. Every case failed at its behavior assertion after successful initial gate tick, CLI answer, and inbox reads; none failed during collection or fixture setup. Failure output follows verbatim:

```text
FFFFFF                                                                   [100%]
=================================== FAILURES ===================================
______________ test_same_facts_produce_zero_reraises_across_ticks ______________
/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/signals/wt/order-out/tests/acceptance/test_role_signal_not_now.py:97: in test_same_facts_produce_zero_reraises_across_ticks
    assert [row["id"] for row in rows] == [first], (
E   AssertionError: Same facts must produce 0 re-raises, even after the old cooldown expires
E   assert ['r1', 'r2'] == ['r1']
E     
E     Left contains one more item: 'r2'
E     Use -v to get more diff
______ test_changed_facts_raise_exactly_one_new_request[binding-changes] _______
/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/signals/wt/order-out/tests/acceptance/test_role_signal_not_now.py:120: in test_changed_facts_raise_exactly_one_new_request
    assert len(added) == 1, "Changed facts must raise exactly 1 new request on the next tick"
E   AssertionError: Changed facts must raise exactly 1 new request on the next tick
E   assert 0 == 1
E    +  where 0 = len([])
_____ test_changed_facts_raise_exactly_one_new_request[idle-lanes-change] ______
/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/signals/wt/order-out/tests/acceptance/test_role_signal_not_now.py:120: in test_changed_facts_raise_exactly_one_new_request
    assert len(added) == 1, "Changed facts must raise exactly 1 new request on the next tick"
E   AssertionError: Changed facts must raise exactly 1 new request on the next tick
E   assert 0 == 1
E    +  where 0 = len([])
_____ test_changed_facts_raise_exactly_one_new_request[ready-work-changes] _____
/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/signals/wt/order-out/tests/acceptance/test_role_signal_not_now.py:120: in test_changed_facts_raise_exactly_one_new_request
    assert len(added) == 1, "Changed facts must raise exactly 1 new request on the next tick"
E   AssertionError: Changed facts must raise exactly 1 new request on the next tick
E   assert 0 == 1
E    +  where 0 = len([])
____________ test_a_different_signal_is_unaffected_on_the_same_role ____________
/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/signals/wt/order-out/tests/acceptance/test_role_signal_not_now.py:144: in test_a_different_signal_is_unaffected_on_the_same_role
    assert len(added) == 1 and added[0]["signal"] == OTHER_SIGNAL, (
E   AssertionError: Only the different signal may raise; the unchanged not-now signal stays quiet
E   assert (2 == 1)
E    +  where 2 = len([{'id': 'r2', 'role': 'architect', 'signal': {'paths': [], 'kinds': ['idle_lanes_with_backlog']}, 'from': 'gate', ...}, {'id': 'r3', 'role': 'architect', 'signal': {'paths': [], 'kinds': ['hot_overlap_files']}, 'from': 'gate', ...}])
_________ test_suppression_survives_a_gate_restart_with_store_reopened _________
/private/tmp/claude-501/-Users-apple-Dev-foragent/4f7c1913-13b4-47f5-9c92-176754d0f0c7/scratchpad/signals/wt/order-out/tests/acceptance/test_role_signal_not_now.py:167: in test_suppression_survives_a_gate_restart_with_store_reopened
    assert [row["id"] for row in rows] == [first], (
E   AssertionError: Reopened state must preserve not-now suppression for unchanged facts
E   assert ['r1', 'r2'] == ['r1']
E     
E     Left contains one more item: 'r2'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_same_facts_produce_zero_reraises_across_ticks
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_changed_facts_raise_exactly_one_new_request[binding-changes]
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_changed_facts_raise_exactly_one_new_request[idle-lanes-change]
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_changed_facts_raise_exactly_one_new_request[ready-work-changes]
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_a_different_signal_is_unaffected_on_the_same_role
FAILED order-out/tests/acceptance/test_role_signal_not_now.py::test_suppression_survives_a_gate_restart_with_store_reopened
6 failed in 3.62s
```


## Publication preview verification

Ran `fa order publish --dry-run order-out/plan.json` with all local write locations isolated under `order-out/.runtime`. It exited 1 because GitHub CLI authentication was unavailable:

```text
foragent: To get started with GitHub CLI, please run: gh auth login Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
```

Network is blocked for this run, so no remote issue lookup or publication was completed. No commit or push was attempted. Independent offline verification called the repository's plan validator and its dry-run publisher with the existing FakeTracker: both succeeded, and the embedded acceptance file exactly matched the staged file. This offline preview is not a claim that the real CLI dry-run succeeded.

```text
Plan (dry run): Not-now role signals stay quiet until facts change
Files: docs/specs/2026-10-31-signal-not-now.md, tests/acceptance/test_role_signal_not_now.py

lane:orch
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| signal-not-now-sticks | Not-now role signals stay quiet until facts change | s | — | no | no |
```
