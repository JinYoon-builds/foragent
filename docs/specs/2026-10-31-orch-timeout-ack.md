# Recognize an orchestrator acknowledgement of the current blocker

## Goal

An orchestrator acknowledgement of an issue's current blocker, including a fa orch note that names the issue and records its diagnosis or parking with a reason, prevents orchestrator-timeout from adding needs-human even after the acknowledgement is more than 30 minutes old. The acknowledgement remains effective while waiting and while the orchestrator records work on other issues. A blocker diagnosed as the same failure reproduced on base/main is a technical wait, not orchestrator silence or a question for the person. A new block needs a new acknowledgement; acknowledging another issue does not cover it. Keep the configured timeout for genuinely unattended blockers and record their timeout escalation once. When such a blocker reaches the person's inbox, its first line identifies an orchestrator timeout rather than a decision.

## Incident and root cause

The supplied 2026-10-31 incident reports #1049, #1062, #1141, #1184 and #1296 waiting for #1295 because the same time-dependent unit test failed on main. The orchestrator had diagnosed the blocks and recorded why with `fa orch note`. These incident facts come from the task, not a live tracker lookup.

On verified main `4afe52354f66b9f2a47b5949d6d3cc3d8c022732`, `src/foragent/scheduler_checks.py::_escalate_overdue_blocker` measures time from the later of the newest blocked comment and the issue's latest run end. Its only acknowledgement exception is a newer parked event. It does not consume notes or their diagnosis. `src/foragent/orch_inbox.py::write_note` overwrites the repository's latest note, without preserving an issue's acknowledgement for this timeout. The acknowledgement context never reaches the gate; elapsed time is mistaken for absence of a response.

The public verbs were read: `note` records current activity; `park` records a reason and removes admission/block labels; `retry` clears the block and queues another attempt; `resolve` records a review verdict and processes the existing PR or queues a retry; `escalate` explicitly adds needs-human. The timeout's generated summary is currently “The orchestrator has not responded to this blocker.” `fa inspect` prints that summary as the inbox's first issue line, so the line does not identify the timeout even though the marker does.

This is missing acknowledgement context at the gate, not missing agent judgment. The orchestrator already supplied the diagnosis. The lane chooses how to make that fact available for the current blocker; this order prescribes no private helper, event shape, text classifier, or implementation sequence.

## Locked public acceptance contract

The standalone draft is `order-out/tests/acceptance/test_orch_timeout_ack.py` and its exact bytes are embedded at `tests/acceptance/test_orch_timeout_ack.py` for publication. It exercises the real `fa orch note`, public `foragent.scheduler.tick` and `fa inspect` entry points with FakeTracker, an injected clock and transport fakes. No timeout, acknowledgement or inbox implementation is replaced in the acceptance file. It runs without network or workers, outside run hours and without approval, so the timeout is independently observable.

1. A current acknowledgement is older than 30 minutes. The issue remains blocked, has no timeout comment and is absent from the person's inbox, both after 31 minutes and after two more hours. A later unrelated note must not erase that acknowledgement. This case covers both an ordinary review blocker and the orchestrator's recorded same-failure-on-main diagnosis. A separate unanswered review blocker in the same tick still times out, so neither global activity nor a blanket timeout removal can satisfy the check.
2. An unacknowledged block stays out of the inbox at 29 minutes, gains needs-human after 31 minutes and produces exactly one timeout comment across repeated ticks. Its public inbox item carries reason orchestrator-timeout, and the rendered first line says timeout rather than decision.
3. An acknowledgement predates a new block on the same issue. At 29 minutes after that latest block there is no escalation; after 31 minutes it escalates exactly once and reaches the inbox. The older acknowledgement cannot suppress a genuinely new unattended block.

The main-CI variant supplies the orchestrator's diagnosis through the existing public note, rather than asking the gate to infer the cause from arbitrary red checks or inventing a new CI evidence format. The ordinary-block variant prevents a special case for a CI keyword or one issue number from satisfying the acknowledgement requirement.

## Ownership, scope and assumptions

One issue, key `orch-timeout-ack`, size s, noninteractive, no dependencies and no new lane or skill choices. Use implicit lane main: the timeout implementation is `src/foragent/scheduler_checks.py`, which matches no ownership pattern in `.foragent.yml`. The orch lane names `scheduler.py` exactly, not its split `scheduler_checks.py`; orch and roles patterns also cover note-related collaborators. The primary defect location determines the lane; no ownership configuration is changed.

The acknowledgement names the issue using the already available note command. A note for unrelated work is not a response to this block. A diagnosed technical wait is acknowledgement of the current block, not a temporary extension of its silence clock. A later blocker starts a fresh unanswered interval. These readings implement the supplied choice-free result and preserve timeout protection.

Fixing the flaky CI test or #1295, editing the five incident issues, changing the configured timeout duration, changing approval or merge boundaries, notification routing, and redesigning CI evidence production are outside this issue. This preparation changes no production source or existing tests.

## Verification on main

The initial checkout HEAD and both main refs were `d535751d12e54f5504f776ad92be6c8c91dfc22f`. Main advanced independently during preparation, so final verification exported exact main `4afe52354f66b9f2a47b5949d6d3cc3d8c022732` inside `order-out/.runtime/main/` without switching branches or modifying the working source. The draft was copied byte-for-byte into that export under order-out/, outside its src/ and tests/. The timeout, note, verb and park source files and .foragent.yml were byte-identical between the original checkout and this main snapshot. The final acceptance and regression commands ran from the exported main with its own source, FakeTracker and test support on PYTHONPATH. The environment used the existing cached Python dependencies with UV_OFFLINE=1, UV_NO_SYNC=1, bytecode and pytest caches disabled, and all temporary writes inside the supplied working directory. The explicit conftest plugin gives the draft outside tests/ the normal isolation fixtures; the published file uses normal discovery.

Ran:

```sh
uv run pytest -q -p conftest -p no:cacheprovider --basetemp=order-out/pytest-red order-out/tests/acceptance/test_orch_timeout_ack.py
```

Exit 1, with three behavior failures and one passing freshness control; no collection or setup errors. Actual failure lines:

```text
E           AssertionError: An acknowledgement of the current blocker remains effective after 30 minutes
E           assert frozenset({'b...needs-human'}) == {'blocked'}
E             
E             Extra items in the left set:
E             'needs-human'
E             Use -v to get more diff
E           AssertionError: An acknowledgement of the current blocker remains effective after 30 minutes
E           assert frozenset({'b...needs-human'}) == {'blocked'}
E             
E             Extra items in the left set:
E             'needs-human'
E             Use -v to get more diff
E       AssertionError: The person's inbox line must name the timeout: [tech-failure] 🧰 Common · #1 Blocked work 1 — The orchestrator has not responded to this blocker.
E       assert 'timeout' in '[tech-failure] 🧰 common · #1 blocked work 1 — the orchestrator has not responded to this blocker.'
E        +  where '[tech-failure] 🧰 common · #1 blocked work 1 — the orchestrator has not responded to this blocker.' = <built-in method casefold of str object at 0x10c1283b0>()
E        +    where <built-in method casefold of str object at 0x10c1283b0> = '[tech-failure] 🧰 Common · #1 Blocked work 1 — The orchestrator has not responded to this blocker.'.casefold
FAILED order-out/tests/acceptance/test_orch_timeout_ack.py::test_acknowledged_blocker_stays_out_of_human_inbox_after_thirty_minutes[review-block]
FAILED order-out/tests/acceptance/test_orch_timeout_ack.py::test_acknowledged_blocker_stays_out_of_human_inbox_after_thirty_minutes[same-failure-on-main]
FAILED order-out/tests/acceptance/test_orch_timeout_ack.py::test_unacknowledged_blocker_times_out_once_and_inbox_line_calls_it_a_timeout
3 failed, 1 passed in 1.54s
```

The unchanged targeted timeout and park checks were also run:

```sh
uv run pytest -q tests/test_orch_blocked_unit.py tests/acceptance/test_blocker_timeout_clock.py tests/acceptance/test_orch_blocked.py tests/acceptance/test_orch_park.py
```

With cache disabled and a checkout-local basetemp, exit 0: `20 passed in 3.29s`. These are regression controls, not a claim that the proposed fix is implemented.

## Break-it verification

Three deliberately incorrect substitutions were tried only in memory on the original checkout (whose timeout and note sources were verified identical to the final main snapshot), each with a targeted `uv run pytest -q` invocation. None changed source or the locked file. Every substitution was caught:

- Omit the timeout effect: the unattended case fails because needs-human is absent (`1 failed in 0.61s`).
- Treat any note as acknowledgement regardless of issue or block time: the new-block case fails with “An acknowledgement older than the latest block is not a response to that block” (`1 failed in 0.71s`).
- Write the timeout comment twice: the unattended case fails with “A timeout must be recorded exactly once”, `assert 2 == 1` (`1 failed in 0.62s`).

The unmodified main run separately detects the wrong acknowledgement result and the missing timeout wording. The same-tick unanswered control rejects a global acknowledgement shortcut; the later unrelated note rejects remembering only the latest repository note.

## Offline publication preview

Publication is limited to `fa order publish --dry-run order-out/plan.json`. The preview uses the real CLI parser, validator and publication planning with an empty, read-only FakeTracker at the tracker boundary, isolated state inside the working directory, and network and child-process execution disabled. It validates the local schema and preview, not live issue collisions, approval eligibility or publication. No approval, commit, push or real publication is requested.


Actual preview result: exit 0. It previewed one unapproved, noninteractive size-s issue on lane main, plus the spec, the new locked acceptance file and the three unchanged existing acceptance controls referenced by Done when:

```text
Plan (dry run): Keep acknowledged technical blockers out of the human inbox
Files: docs/specs/2026-10-31-orch-timeout-ack.md, tests/acceptance/test_blocker_timeout_clock.py, tests/acceptance/test_orch_blocked.py, tests/acceptance/test_orch_park.py, tests/acceptance/test_orch_timeout_ack.py
lane:main
orch-timeout-ack | Honor acknowledgement of the current blocker before escalating an orchestrator timeout | s | no dependencies | unapproved | noninteractive
```

Only `order-out/plan.json` and `order-out/tests/acceptance/test_orch_timeout_ack.py` are deliverables; temporary verification adapters, logs, caches and test data are removed after verification. All changes remain uncommitted.
