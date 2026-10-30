# One orchestrator seat (r48)

## Decision and incident

Jin's final decision, 2026-10-30, with QA's compatibility clarification: exactly one orchestrator seat per project, paired with the planning session. A project-state lease records pid + process start_time and session_id when known. Check its lifetime with processes.recorded_identity_evidence. A live lease holder adds spawn suppression even when its heartbeat is stale or describes another session. Preserve all existing spawn suppressions exactly as today. The lease alone identifies the seat holder for watch --follow refusal, refresh handover and #952's silence episodes.

Incident r47: the gate spawned a second orchestrator at 02:28Z while an older claude --bg orchestrator was alive. _session_alive missed it because the heartbeat recorded another identity. The missing durable owner is the root cause. Retain the existing evidence checks and add this missing ownership evidence. A stale heartbeat alone never warrants a second session while a live holder or any other existing live evidence remains.

## Spawn suppression and authoritative ownership

The existing independent spawn inhibitors are a fresh heartbeat, a live recorded heartbeat session checked by pid/start_time, and a live /fa-orchestrator process at the repo root. Any one still inhibits gate and explicit-ensure spawning, even when the lease is absent, dead or reused. Add a live lease lifetime as another inhibitor. Ordinary spawning becomes eligible only after all these facts clear, subject to today's work/off/spacing conditions. Do not expire an alive lease on heartbeat age.

Those existing facts inhibit spawning without assigning a seat holder. Only the lease supplies holder identity: a free/dead/reused seat can be taken by watch --follow; a different live lease holder causes immediate nonzero refusal with one localized line. fa orch seat --json reports lease holder liveness, not the aggregate eligibility of a gate spawn. #952 must not build a holder or silence episode from legacy evidence alone.

## Intentional refresh and shared plumbing

`#413` is merged in src/foragent/orch_refresh.py. fa orch refresh --ready intentionally starts a new session while the old one is alive, after its existing handoff and conversation checks. Hold the same project opener lock from spawn through lease handover, write the new holder there and retain the complete outgoing holder as previous. The outgoing session's later follow watch refuses and cannot replace or renew the fresh holder. Preserve refresh's active/retired heartbeat descriptors, events and public launch seam. The shared launcher is in orch_spawn_launch.py and explicit ensure is in orch_ensure.py; those existing paths are included so the additional lease guard reaches the actual launch paths. Keep #951's already-ordered Orca tab beside the planning session's recorded handle and today's screen/focus behavior.

## Work split and dependency

One new issue: key orch-seat, lane orch, size m, title 'One orchestrator seat per project: a lease checked by process identity'. Done when: uv run pytest tests/acceptance/test_orch_seat.py. One edit: existing #952, lane orch, held open PR #958. Its full Goal retains the valid setting, localization, notification retry and question-channel requirements, while replacing its independent session identity tracker with orch_seat. Silence origin is max(heartbeat at, holder taken_at). Builds on the orch-seat issue; wait for it (key orch-seat). The edits schema does not allow blocked_by; the dependency is stated in the Goal and checklist for scheduling. No remote issue or PR is changed during this preparation.

## Acceptance wire choices and assumptions

Use orch-seat.json under the StateStore root. It stores holder={pid, start_time, taken_at, session_id when known}, with previous holding the outgoing holder on refresh. The CLI reports holder/previous plus alive; absent lease means holder=null, alive=false and inspection creates no lease. That result does not override any existing spawn inhibitor. Timestamps are timezone-aware ISO8601; start_time retains the process identity helper's numeric representation. Same-holder watch re-entry retains taken_at. Reuse the current nearest-Claude reader at watch entry. Unreadable identity is not proof that a lease holder is gone. Internal Python APIs and unspecified details remain the implementer's choice.

## QA classification and scope

The QA item is a planning judgment correction, not a tool bug. The missing context was the distinction between evidence that suppresses a spawn and identity that assigns a seat. This order now provides that distinction explicitly and tests it; no production meaning classifier, script-hosted LLM or judgment rule is introduced.

Deliver only order-out/plan.json and the two order-out/tests/acceptance copies. acceptance_files embeds their complete final contents under eventual tests/acceptance paths. No feature implementation, original src/ or tests/ edit, commit, push, reset, checkout, stash or real publish is part of this run. Keep every existing acceptance test function name, including the historical name containing 'spawns_despite_fresh_heartbeat': it now checks suppression first and spawning only after the evidence clears.

## Issue #952 source and retained requirements

The original full rewrite was based on https://github.com/JinYoon-builds/foragent/issues/952, updated_at 2026-10-30T05:30:38Z. The requested gh issue view 952 --json body -q .body failed to connect; the connected GitHub fetch_issue tool supplied the complete body. This QA update changes the spawn/ownership distinction without dropping its still-valid requirements: default-30 team setting with inclusive range 0..1440 and SETTINGS/count-test updates; localized title, description and push; inclusive alert threshold; successful-send-only durable deduplication and retries on False/exception; hourly diagnostics; fresh observed heartbeat rearm even without work; no-work/no-fresh does not rearm; off disables the entire silence check; shared gate/explicit-ensure silence function with unchanged explicit output; and the visible-question skill sentence. Existing gate liveness scans remain spawn inhibitors; #952's own episode identity/origin scans and comparisons are removed.

## Existing locked-test compatibility

All four existing locked modules remain unchanged and their expectations remain valid. No locked expectation needs replacement for this order. In particular:

- test_orch_spawn.py:188, test_orch_spawn_skips_fresh_heartbeat_disabled_spawn_and_no_approved_work: `assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None`. A fresh heartbeat still suppresses a spawn without a lease; off/no-work behavior also stays.
- test_orch_spawn_liveness.py:158, test_orch_spawn_liveness_repo_claude_suppresses_spawn_and_throttles_event: `assert_alive_and_throttled(case, commands, pushes)`. The repo-root /fa-orchestrator process still inhibits a spawn without a lease and retains hourly diagnostics.
- test_orch_spawn_liveness.py:173, test_orch_spawn_liveness_recorded_session_survives_a_stale_watch_heartbeat: `# The stale heartbeat's recorded PID plus start time is the sole evidence.` That live recorded lifetime still inhibits a spawn without assigning seat ownership.
- test_orchestrator_by_default.py:129, test_orchestrator_by_default_ensure_does_not_call_spawn_for_a_live_orchestrator: `assert calls == commands == pushes == []`. Explicit ensure retains its fresh-heartbeat short circuit.

The other locked refresh, screen-selection, session-id and cooldown expectations also stay. Preserve the public refresh descriptor seam used by existing fakes while enriching real launch identity as needed. No original locked fixture is edited in this order.

## Verification approach

Tests run offline against the unchanged source in this worktree, reusing git_history, liveness_case, fake_processes, fixed clocks, the CLI binder and launcher/notification fakes. New seat tests retain each existing spawn inhibitor with absent and dead/reused leases, clear the evidence, and then assert that the actual spawn persists the new holder. After spawning, incidental argv/cwd evidence is removed so the subsequent reopening check proves suppression by the lease. The silence copy also proves that each legacy inhibitor can block spawning without assigning a holder or sending a silence push.

The existing dependency environment is selected explicitly with uv run --no-project --offline --python /Users/apple/Dev/foragent/.venv/bin/python; no dependency installation is needed. PYTHONDONTWRITEBYTECODE=1 and pytest's cache provider is disabled. TMPDIR and --basetemp are confined to temporary order-out/.verify paths, removed before delivery. Staged tests load tests/conftest.py with -p conftest and PYTHONPATH includes src, tests, order-out/tests/acceptance and tests/acceptance; the published Done when commands use normal tests/conftest.py discovery.

## Verification results

Re-ran all four unchanged locked modules: 56 passed in 9.48s. Re-ran the updated seat module: 20 failed in 3.43s. Re-ran the edited silence module: 12 failed, 3 passed in 2.57s. These are feature failures with no collection/fixture errors. In the absent/dead/reused lease cases, today's suppressions pass their assertions; after the evidence clears, main fails to create or replace the lease. The other seat failures expose missing lease liveness, follow refusal/taking, JSON inspection and refresh handover. The silence failures expose its missing lease prerequisite, setting and alert. The three passing silence cases retain ordinary spawn behavior and demonstrate that legacy evidence can still inhibit a later spawn without assigning a seat or producing a silence push.

Representative failure lines copied from these current runs:

```text
E   AssertionError: An orchestrator launch or accepted follow watch must persist orch-seat.json
E   AssertionError: A live lease holder must prevent a second session despite a stale, unrelated heartbeat
E   AssertionError: A replacement launch must persist the new holder's PID and start_time
E   AssertionError: A different live seat holder must refuse watch --follow
E   AssertionError: fa orch seat --json must report the lease (exit=2): usage: fa orch [-h]
E   AssertionError: refresh --ready must replace the lease holder even while the outgoing holder is alive
E   AssertionError: fa settings list must register orch_silent_alert_minutes
E   AssertionError: The 30-minute threshold is inclusive and ignores newer legacy identity facts
```

Strict read_plan/validate_plan checks and the public order.publish(..., dry_run=True) with an in-memory #952 tracker passed. Both acceptance_files values were regenerated from and matched the staged files byte for byte; AST syntax/docstrings and all 15 prior test function names (7 seat, 8 silence) were verified unchanged. The four original silence test function names are retained as well.

Exact pytest invocations used for this QA rerun (create order-out/.verify/tmp before repeating them):

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/order-out/.verify/tmp" PYTHONPATH="$PWD/src:$PWD/tests:$PWD/tests/acceptance" uv --no-cache run --no-project --offline --python /Users/apple/Dev/foragent/.venv/bin/python python -B -m pytest -p no:cacheprovider --basetemp="$PWD/order-out/.verify/qa-baseline" tests/acceptance/test_orch_spawn.py tests/acceptance/test_orch_spawn_liveness.py tests/acceptance/test_orch_refresh.py tests/acceptance/test_orchestrator_by_default.py -q
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/order-out/.verify/tmp" PYTHONPATH="$PWD/src:$PWD/tests:$PWD/order-out/tests/acceptance:$PWD/tests/acceptance" uv --no-cache run --no-project --offline --python /Users/apple/Dev/foragent/.venv/bin/python python -B -m pytest -p no:cacheprovider -p conftest --basetemp="$PWD/order-out/.verify/qa-seat" order-out/tests/acceptance/test_orch_seat.py -q --tb=short --assert=plain
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/order-out/.verify/tmp" PYTHONPATH="$PWD/src:$PWD/tests:$PWD/order-out/tests/acceptance:$PWD/tests/acceptance" uv --no-cache run --no-project --offline --python /Users/apple/Dev/foragent/.venv/bin/python python -B -m pytest -p no:cacheprovider -p conftest --basetemp="$PWD/order-out/.verify/qa-silence" order-out/tests/acceptance/test_orch_silent_reaches_person.py -q --tb=short --assert=plain
```

The source worktree is pinned to a24645460667a8a16180cac3cf80a14f7c330e2c, which was main at initial preparation. At this QA rerun origin/main is 7456517f0d6276a2639e1c287c07187e309fb482; only docs/decisions.md differs. The implementation, original tests and skills are identical across those refs. No source modification was made to produce these results.

## Open questions

None. Existing locked expectations are preserved. The #952 prerequisite remains expressed in its Goal/checklist because edits cannot encode blocked_by.

## Implementer discretion

Any detail not fixed here or in the acceptance tests is the implementer's choice: take the smallest change that keeps the listed tests and today's behaviour elsewhere, use the messages registry for new human text (English and Korean), and state each choice in the PR.