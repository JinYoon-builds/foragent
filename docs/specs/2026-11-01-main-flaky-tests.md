# Deterministic main CI: remaining review conflict / usage-limit race

## Checked base and scope

HEAD and the local origin/main both equal e6d4f18356d128c28c456f41f00d5ff01922cb68. A read-only GitHub main-branch query independently returned that same SHA (2026-11-01T07:38:11.278Z). The checkout refs were not moved; no git commit, push, reset, checkout or stash command or repository publication was performed. Temporary Git histories used by the existing tests remained inside the draft runtime.

Architect r225 reported five red main episodes over seven days and the failed-run counts in the request: presence 44, notify retry 21, prepared results 16, conflict versus limit 5. These are supplied incident counts, not counts recomputed by this investigation. The supplied episode SHAs/times are 74511c90 (10-27 09:00-09:35), 9129a3f0 (10-30 08:43-09:11), 42d97182 (10-31 08:06-09:02), cf40ab56 (10-31 12:01-13:30), and d45cb7bc (11-01 06:52). Keep this historical evidence separate from the status of the checked base.

Only one unresolved independent root cause remains. This is a local order draft, not implementation or publication. No src/ or tests/ file was changed during preparation. No new or replaced locked tests are required. Existing unit tests are sufficient to express the behavior, and acceptance_files is omitted.

## Per-test findings

| Reported test | Root cause and current code | Current status / local repetition |
| --- | --- | --- |
| tests/test_presence.py::test_a_lone_surrogate_in_one_field_replaces_only_that_character (all 7 fields) | Fixed-date store notices were judged against the prompt hook's host clock, so the 24-hour notice window could remove the very row whose UTF-8 replacement was asserted. _hook now receives the fixture's now and patches statusline.datetime; the hook passes that time into presence.prompt_context, then recent_notices(now - NOTICE_WINDOW). #1295, commit 466cf366, contains the fix. The current stall_store also pins the hook clock. This was a recency-fixture issue, not a defect in replacement of the surrogate. | Already fixed; all seven fields passed on every one of 30 separate pytest launches (210 cases). Drop from implementation scope. |
| tests/test_presence.py::test_a_line_separator_inside_a_field_keeps_the_notice_whole | Same hook-versus-fixture recency clock mismatch; #1295 now passes the fixture time through _hook. Assertions still check the retained U+2028 channel and whole notice. | Already fixed; 30/30 passed. Drop. |
| tests/test_presence.py::test_a_lone_surrogate_in_a_raw_notice_line_reaches_the_turn_replaced | Same recency clock mismatch; #1295 passes now into _hook. The strict UTF-8 output/replacement assertion is unchanged. | Already fixed; 30/30 passed. Drop. |
| tests/test_cli.py::test_inspect_cli_notify_logs_failure_and_retries_then_dedupes | app pins cli.datetime to NOW, and commands.inspect passes that clock into InspectContext. inspect_actions.notify compares ctx.now().astimezone().hour with notify_summary_hour. The old test chose its off-summary hour from the host's datetime.now(), which could coincide with the fixed command hour and unexpectedly emit a daily summary. Commit 8808c00e (#1192) derives the off-hour from NOW instead. Commit 80f35d13 (#1252) only adds the explanatory comment; do not attribute the functional fix to that comment. | Already fixed; 30/30 passed across time zones. Drop. |
| tests/test_cli.py::test_inspect_renders_card_prepared_results_and_language (text and JSON) | FakeTracker merges at fixed NOW, while inspect_data.collect applies ctx.now() minus seven days. A real command clock ages the fixture PR out of that window. The current test and app both patch cli.datetime, and commands.inspect resolves that patched facade when constructing the context. Commit 7df806f3 (#421) already pins this test's clock; d7a8d138 also adds an equivalent local Clock patch. | Already fixed; both parameters passed on every one of 30 launches (60 cases). Drop. |
| tests/test_review_conflict_paths.py::test_panel_member_conflict_wins_over_another_members_usage_limit | ConflictThenLimitPanel assigns responses from shared call arrival order: PanelExecutor indexes self.calls, and its subclass chooses the usage-limit call by membership in self.reviews[1:2]. Actual review member 2 can arrive before member 1. More importantly, each panel member uses _available_executor, whose hard-limit and launch-hold paths call _limit_wait. That mutates the one issue's ready/running labels and board before _panel_review has chosen the aggregate result. Several later reviewers can observe the same recorded limit and concurrently requeue the issue. RecordingTracker then queues writes behind an in-flight or pending issue intent. The subsequent spec-question block/comment can remain pending after the runner has finished with spec-question. | Still flaky on this main: 29 passed, 1 failed in 30 unmodified launches. The failing assertion is the absent question comment (IndexError), not the spec-question outcome or the merge suppression. One additional observed failure confirmed the pending label writes and the exact conflict comment in the outbox. |

## Evidence for the remaining root cause

A read-only inspection of the actual d45cb7bc push run confirms the same failure:
https://github.com/JinYoon-builds/foragent/actions/runs/41282572191/job/124777311535
The job reports tests/test_review_conflict_paths.py::test_panel_member_conflict_wins_over_another_members_usage_limit failing at questions(case.tracker)[0] with IndexError. The preceding outcome == spec-question and panel.merges == [] assertions had passed. The job reports 1 failed, 1207 passed, 1 skipped, 62 deselected. No CI job was rerun.

On current HEAD the ordinary single-test loop reproduced this exact failure at iteration 11, with TZ=Asia/Seoul, LANG=LC_ALL=C and PYTHONHASHSEED=10. It needed no monkeypatch, tracing or source change.

An additional diagnostic run used a shorter Python thread switch interval (0.000001 seconds) and a pytest observer outside src/ and tests/. The observer only captures the existing case/panel and reads comments, outbox and calls when the unchanged test fails; it does not replace any production decision or result. Three runs passed and the fourth failed. That failure showed:
- the conflict response was associated with actual member 2, and the limited response with actual member 1;
- the remote fake tracker had no comments;
- the outbox contained multiple ready/running label operations, then blocked and ready/running removal operations, then the exact blocked reason=spec-question comment including CONFLICT;
- the run was already finished with outcome spec-question.

Therefore merely assigning fake replies to stable identities, delaying an assertion, flushing the outbox in the test, or forcing a fixed production worker order would conceal the shared issue lifecycle race. Fix the owner of round-level effects so a losing member outcome cannot create pending requeue work ahead of the winning conflict. Keep transport/usage facts and final round handling consistent. Stable fake member identities and explicit scheduling controls make this same behavior reliably testable; they are not a substitute for the production fix.

## One small issue

Lane review-engine owns src/foragent/run_review.py per .foragent.yml. Size s, no blockers. Expected changes:
- src/foragent/run_review.py
- src/foragent/run_execution.py
- tests/test_review_conflict_paths.py

The named test must preserve all of its assertions: spec-question outcome, no findings merge, the conflict in the question comment, and SPEC-CONFLICT in review events. Other existing test assertions retain their behavior. A panel with no conflict must still honor a real usage limit, quota recording and the final ready/requeue outcome. Single-reviewer behavior, reviewer fallback, notes, shared admission and normal concurrency stay covered by the listed existing unit regression files. Use ordinary unit scheduling cases for both arrival/completion orders and for later queued reviewers observing a recorded limit. Keep production aggregation and lifecycle decisions real.

No semantic classifier, keyword policy, LLM call in a script, or new agent execution order is part of this issue. This is structured lifecycle and test-fixture repair.

## Verification performed during preparation

Python 3.12.13, pytest 9.1.1, macOS arm64; existing .venv used offline via uv run --no-sync --offline. pytest-repeat and pytest-randomly are absent. Every reported node was launched by itself 30 times with -p no:cacheprovider -p no:randomly. Runs cycled UTC, Asia/Seoul and America/Los_Angeles; C and en_US.UTF-8 locales; hash seeds 0 through 29; PYTHONUTF8=1 retained UTF-8 I/O. That is 180 processes and 390 cases: 389 passed, 1 failed. The five fixed node groups accounted for 360 passing cases.

Each of the three files was also run separately once: presence 74 passed; CLI 84 passed and 1 unrelated failure; review conflict paths 6 passed. The CLI failure is test_tick_returns_while_notification_child_is_still_running: expected no actions, got ['Pushed: #1']. A prior combined three-file run had the same unrelated failure (164 passed, 1 failed). It is recorded, not included in this order and not claimed green.

The initial six-node smoke run passed all 13 cases. The additional observer stress experiment stopped at its first failure (3 passed, 1 failed), so it is not a 30-run stability claim. verification.json retains commands, environment variants, outputs and observer source. No full suite or CI shard was run.

## Completion check

Done when includes 30 independent launches of the named remaining test (3 time zones x 2 locales x 5 seeds), its file once, and the related existing review/recovery unit files once. Future implementation must pass these; this preparation run intentionally leaves the reproduced failure unfixed. An all-green loop alone is insufficient if concurrency or assertions were weakened; the ordinary unit schedule controls must expose the competing limit and conflict.

Out of scope: already fixed presence/CLI items; the unrelated CLI baseline failure; global tracker/outbox redesign; review policy, quota policy or concurrency reductions; changes to locked acceptance tests; assertion weakening, retries, xfails or skips; other drafts. Only local draft artifacts are delivered.
