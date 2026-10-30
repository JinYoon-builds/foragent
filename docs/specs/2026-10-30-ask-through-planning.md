# Questions go through the planning session (r50)

One issue: key `ask-through-planning`, lane `orch`, size `s`. Planning/order work only in this delivery; implementation follows in the issue.

## Evidence and root cause

Read the supplied CTXDIR/r50.txt and r38.txt offline. r50 records Jin's one-head instruction: every question goes through planning, including merge approvals and order cards. r38 records an invisible background orchestrator waiting about 15 hours on AskUserQuestion. Its separate view/launch and silence-alert work is not added to this small issue.

- src/foragent/commands/_orch_box.py accepts no ask kind; orch_inbox.ask stores none.
- orch_inbox.route_question returns early for a live planning heartbeat and otherwise calls notify.send. A live heartbeat currently suppresses push but the CLI produces no actionable planning handoff. The cross-session transport exists as the agent's SendMessage instructions in the skills; there is no Python push-to-planning sender in this checkout.
- fa plan heartbeat writes the 120-second marker, but statusline.run/session_start and presence do not renew it. The existing watch_open.record_terminal already identifies planning structurally by excluding any FORAGENT_ROLE and <state>/worktrees; reuse those facts rather than classifying text.
- orch._watch_snapshot reads local issue sets and queue state only. add_request and answer durably write their records/events, but neither change a watched fact, so Monitor receives no output.
- The main skill and merge reference still direct the orchestrator to AskUserQuestion for the person. The missing ingredients are the correct communication context, an actionable handoff and observable inbox events, not another meaning classifier.

## Goal

Route all D71 questions for the person, human merge approvals and order cards through the planning session. Add `fa orch ask --kind question|merge|order`, default question, and persist kind alongside the existing ID, options, recommendation, status and answer. With the existing planning heartbeat fresh (inclusive through 120 seconds), surface an actionable planning handoff with the question ID, full question and choices, and use the existing agent-owned SendMessage cross-session path; do not silently suppress delivery. When the heartbeat is absent or stale, preserve exactly one configured push per newly asked item. `fa orch answer ID --text ...` closes only that item, preserves its kind and records the person's answer.

Update skills/fa-orchestrator/SKILL.md and references/merge.md, order-card.md and other affected routing references consistently: AskUserQuestion in the orchestrator's own session is used only while the person is talking directly there. Otherwise the orchestrator records a question with fa orch ask (kind merge or order where applicable), sends it to planning, and acts on the recorded fa orch answer. Merge still requires the person's explicit Merge choice, recorded by the answer, before fa orch merge with attribution; neither sending a question nor other agents' text grants permission. Keep the existing D71 decision boundary and dream-merge exception.

Renew the same fa plan heartbeat from the Claude hook integration Foragent already installs in statusline.py/presence.py, for planning sessions only: no FORAGENT_ROLE session (including an empty value), no gate worktree. Reuse the existing 120-second freshness semantics and local state; no LLM, network or new polling service in the hook. Exercise repeated renewal after expiry. Preserve existing statusline bytes, session-start time, presence switches and the locked read-only snapshot contracts; an installed UserPromptSubmit hook through the existing installer is the compatible recurring route (see locked-test conflicts in the spec).

Make a running fa orch watch --follow emit one JSON line per new inbox request ID and per newly answered question, including two requests created between the same two polls. The new ID/answer must wake an existing Monitor without another prompt. Unchanged polls and request progress notes remain quiet; keep existing snapshot, heartbeat, seat, failure/recovery and offline behaviour. Route by stored facts and explicit kind, leaving interpretation, approval choice and SendMessage use to the agent.

Any detail not fixed here or in the acceptance tests is the implementer's choice: take the smallest change that keeps the listed tests and today's behaviour elsewhere, use the messages registry for new human text (English and Korean), and state each choice in the PR.

## Scope and assumptions

1. “Existing push-to-planning path” means D76's existing agent-owned SendMessage path. The CLI exposes the durable question ID and complete question/choices for that handoff; the skill sends it to planning, which asks the person and records fa orch answer. The acceptance test observes this public handoff plus the absence of a fallback push. It does not claim to execute Claude's SendMessage or prove delivery to a real session offline. Do not invent an undocumented Claude CLI sender or embed an LLM decision in Python.
2. The recurring renewal may use an installed UserPromptSubmit command from the existing Claude hook installer. The test discovers the actual installed command instead of prescribing a new CLI name, and exercises it together with the normal statusline entry point. SessionStart alone does not cover later expiry. No idle polling daemon or new session framework is required.
3. Watch output retains the existing JSON-lines interface. Its new event fields and persistence layout are the implementer's choice; tests require identity, one line per event, no replay during unchanged polls, and compatibility with today's seat/snapshot contracts.
4. r38's terminal placement, background launcher and long-silence recovery remain sibling work; do not add thresholds or blocked-state classifiers here. Existing merge readiness/attribution rules and dream exceptions remain in force.

## Locked acceptance

Publish the exact bytes of `order-out/tests/acceptance/test_ask_through_planning.py` as `tests/acceptance/test_ask_through_planning.py`; the same bytes are embedded in acceptance_files. Its fixtures come from the existing offline routing, presence hook and watch/seat acceptance tests. All kind values and omitted-kind compatibility are tested at fresh (120 s), stale (121 s) and absent heartbeat states. Answer closure, role/gate exclusions, recurring hook renewal, simultaneous new requests, answered-question wake-ups and the skill/card route are locked.

## Locked-test conflicts and compatibility

A direct heartbeat write on every statusline read conflicts with current locked snapshots, even though their assertion prose mentions session/report time:

- tests/acceptance/test_presence_session_start.py: `assert _local_files(case) == recorded, "Brief and statusline must not rewrite the hook's session start time"`. _local_files includes the entire repository state store and statusline data directory.
- The same module: `assert _local_files(case) == before, "A disabled SessionStart and statusline must not record a new session time"`.
- tests/acceptance/test_fa_report_since.py: `assert snapshot(case.store) == before, "Normal brief and statusline must not create a report marker"` and `assert snapshot(case.store) == reported, "Reading brief or refreshing statusline must not advance the report time"`. snapshot includes every state file, including plan-heartbeat.json.

Use the existing hook installer to renew on UserPromptSubmit rather than editing those locked tests or weakening these checks. No conflict was found between the planning route and existing question-routing/merge permission tests: fresh planning must still produce no phone push, and merge must still wait for a person's choice. New tests impose no Python judgment policy.

## Verification report (order preparation, no implementation)

Read `fa order publish --help` for the rejecting-unknown-fields schema. Both issues and edits are supplied. Source and existing tests are untouched. Final validation passed using foragent.order.validate_plan, exact acceptance-byte comparison, Python compile(), the required Goal suffix check and foragent.order.publish(dry_run=True) with FakeTracker (zero issues created). git diff --check passed; git status showed only order-out/ as untracked.

Current source: 3bfe7d12a599b5d81cb1b2e62d9e427cabe7e846. Main baseline: 5a1220a302b93238965a022f310d353e05fc0f16, extracted read-only with git archive inside order-out/.runtime (no checkout). Commands used the existing /Users/apple/Dev/foragent/.venv/bin/python, PYTHONDONTWRITEBYTECODE=1, PYTHONPATH=src:tests:tests/acceptance, -p no:cacheprovider, and temporary directories confined to order-out. New staged tests additionally use `-p conftest` to load the existing tests/conftest.py outside its normal subtree.

New module, current source: `python -m pytest -p conftest -p no:cacheprovider order-out/tests/acceptance/test_ask_through_planning.py -q --tb=no` with an order-out --basetemp: 16 failed, 5 passed. The five passing cases cover exclusion of role sessions and gate worktrees; all failures are missing feature assertions.

The same final module on the archived main baseline: 16 failed, 5 passed. A focused --tb=short run on main printed these feature failures:

```text
fa: error: unrecognized arguments: --kind merge
E   AssertionError: Planning session hook must renew fa plan heartbeat
E   AssertionError: watch --follow must print new request r1 once
E   AssertionError: watch --follow must print the newly answered question once
E   AssertionError: AskUserQuestion must be limited to a person talking in the orchestrator's own session
```

Default-kind cases also printed `AssertionError: Persist kind; omitted --kind means question` before their routing assertions. No missing fixture/import/collection errors remain.

Related locked modules (first 12 listed in Done when): 71 passed, 7 failed on both current source and the unchanged main archive. The same 7 failures are an environment limitation caused by keeping test temporary directories beneath this configured worktree: presence.project_root walks ancestors and finds the enclosing .foragent.yml, so the fixtures intended to be outside any project receive a presence suffix. This is not a change introduced by this order, and this report does not claim the full regression run passed. No existing locked tests were edited. Failure locations and quotes:

- test_presence_statusline.py:172: `AssertionError: Outside a Foragent project, stdout must be byte-identical` (1 case).
- test_statusline_wrapper.py:127: `AssertionError: statusline stdout must be byte-identical, without decorations or decoding` (exit codes 0 and 23).
- test_statusline_wrapper.py:171: `AssertionError: collecting usage must not add anything to the user's statusline` (1 case).
- test_statusline_wrapper.py:191: output bytes differ by the presence suffix (absent and preserved usage cases).
- test_statusline_wrapper.py:293: output bytes differ by the presence suffix (existing-install/double-wrap case).

Additional related locked modules test_fa_report_since.py and test_statusline_uninstall.py: 19 passed on current source. Aggregate current-source related locked results: 90 passed, 7 baseline/environment failures. The implementer's listed Done when checks must pass in the normal isolated test environment as well as the new feature tests; this order does not authorize fixing unrelated test fixtures.

## Open questions and limits

No blocking product question. Real cross-session delivery is outside this offline test's observable boundary; the existing SendMessage path is the explicit assumption above. If a Python planning-delivery adapter exists only in another branch/environment, reuse it and review the acceptance boundary through the test owner's re-approved order, rather than inventing an API here. The seven outside-project baseline failures require a normal temporary location outside a configured project to assess their original contract; all file writes in this run were kept within the assigned worktree.
