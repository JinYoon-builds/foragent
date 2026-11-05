# orch lane memory

Curated 2026-11-02. docs/rules.md wins over anything here. Lessons only; the state of work lives in GitHub issues.
Module line caps are gone (order 2026-10-31-no-line-caps), so line-cap remarks in older `project_*.md` notes are out of date. Only `skills/fa-orchestrator/SKILL.md` keeps a limit: ≤150 by `splitlines`, no trailing newline (`wc -l` says 149). Move prose no test regex reads to `references/` and compare PASSED/FAILED of every acceptance file reading the skill ([#674](project_674_skill_space.md)).

## How issues go wrong

- Rounds (10-30 to 11-02): #466 27, #154 22, #1004 18 and still held, #1105 18, #993 14, #1186 11, #435 10; most merges took 1 to 3. Long runs were the next instance of one class, not new defects.
- #1105 proves redesign (D106): 7 rounds patching a replay of the user's test command on base ended when base evidence came only from the run baseline. Same class twice: stop patching, name the one source or decision point, or raise QUESTION.md.
- Classes reviewers repeat here; search for all before the first review:
  - Missing evidence read as permission or "nothing to protect" (#1004, #1105, #459). Fail closed.
  - Effects before authorization or the durable record (#1185 hint before comment, #1004 slot before ownership check).
  - Cleanup or a later step overriding the outcome (#466 `finally` push replacing a hold, #1105 protected run overwriting a refusal).
  - Partial composite writes (#1208 comment posted, close failed): finish the rest without repeating.
  - Guarded identity differs from measured identity (#466 SHA, #1140 same tree is not provenance, #993 status is not a reservation).
  - Stale reads ordered by arrival (#1578, #1525 acknowledgement order is not effect order, #1185 two reads of a run). Test each state change between reads.
  - Exits that skip the record (#1579, #604). Put the write on a wrapper every exit passes.
  - Elapsed-only stops (#435, D89).
- A surviving mutant often means a redundant second decision point: delete it (#1184, #1560). NameError mutants prove nothing; return a wrong value (#1559). Under an Exception-swallowing hook, arrange a following effect the exception would lose (#1578).
- Mutate only a source copy (`PYTHONPATH=<copy>/src fa test ...`, confirm `module.__file__`). Mutant loops: nohup, per-mutant timeout, log under `.foragent/`, `pgrep -fl` for leftovers.
- A helper choosing inputs for a reader must not add its own filter (#1597). After removing logic, sweep dead parameters, arguments, imports (#1182).
- A new phase also needs `WAIT_PHASES` copy in `tests/test_watch_scene_contracts.py`, `messages/` words, and the need set in `orch_facts_resources.py`: grep sibling phase names as text (#1597).

## Locked tests and questions

- A locked test that contradicts the Goal or another locked test: copy it to `tests/` (the classifier refuses probes in `tests/acceptance/`), change the one line, run, delete, raise `.foragent/QUESTION.md` with options and a recommendation. Seen in #238, #435, #995, #1027, #1041, #1105, #993, #1186, #1183.
- REBUTTAL alone loops back to verification (#1183); `run_implementation` reads QUESTION.md first and stops as spec-question. Quote a spec answer in later rebuttals and pin it with a test (#1105).
- Reviewers can cite an older head: compare with `HEAD` (#1597). Reviews against a new base read added files as "deleted" (#248). "File moved" hints can be false: compare blobs from the merge base (#1182). Keep lists out of REBUTTAL.md, it can trigger review-dispute (#1172).
- Prove "fails on base" by ID sets: `git archive <merge-base> src` + `PYTHONPATH`; drop test files importing new modules, or collection errors void it (#1560). Newer results retire old blocker explanations (#1105).
- `tests/acceptance/test_flow_scorecard_split.py` was still red on base at #1597, and `test_*split*.py` globs include it.
- A locked test importing an unmerged helper breaks collection on main and every PR (`tests/merge_path_expected_red.py`, #1182). Orders must publish the helper with the test.
- `test_i_full_machine_budget...[group-check-and-return]` can fail "needed an external slot release" under load (2 of about 10; cause unconfirmed). Keep run logs.

## Running and verifying

- `fa test` in the worker sandbox often stops with EPERM on `test-slots.lock`. Do not fake `FORAGENT_TEST_SLOT=held`. Say "not verified here" (the outer runner still runs checks: read the summary in `logs/issue-N.log`), or hold the same slot through a read-only fd with real `hold_test_slot` limits and `SLOT_POLL_SECONDS`. Old `check-done.log` passes are history.
- Done when runs via `/bin/sh -c` without the gate venv; this Mac has only `python3`, so `python -m pytest` always fails: raise QUESTION.md (#1172).
- Scratch goes under `.foragent/`: the goal hook counts `/tmp` as outside the repo. Long runs: background plus log, tools kill at 10 minutes. One wrong path in a related-test command runs nothing. Full unit suite about 33 minutes. In zsh, split lists with `${=T}`.
- Recreation can remove `.foragent/TRY.md`, `REBUTTAL.md`, `SEARCHED.md`; new lane files join the keep-list in `run_conflicts._recreate_worktree`. On any resume: `git status`, `git log HEAD..origin/main`, HEAD's parents, keep commits (#1105, #1185, #1141, #1184, #1345). Do not merge main by hand: `follow_base` does; resolve in the worktree, `git add`, leave the commit to the runner.
- Integration with read-only Git metadata: `git clone --shared` under `.foragent/`, merge exact SHAs, run with `PYTHONPATH=<clone>/src`.
- `git checkout <file>` is refused: `git show HEAD:<path> > <path>`. Conftest blocks `git commit`/`checkout` in tests: use `git_history.update()` or plumbing (`commit-tree`; `GIT_INDEX_FILE` + `update-index --cacheinfo` + `write-tree`) with a bare `origin`.
- Conftest neutralises real kills, sleeps and ancestors; `local_cli` patches `orch.time.sleep`. `subprocess.run(timeout=)` can poll via sleep: use Popen + `threading.Timer` + `communicate()` there (#1560). No-repo fixtures forbid Popen: check `.git`/`GIT_DIR` first, return unknown. Never reproduce signals on the real machine.
- Tests: advance frozen clocks between runs (history dedups on `(issue, started_at)`); set `observed_at` older than `now`; force interleavings with an injected clock or Barrier, storms miss ordering (#993, #1581). New Config keys change `test_exact_defaults`.

## CI

- PR CI runs on the main-merged tree. Empty "CI failed" usually means jobs still run: `gh run view <id> --json jobs`, then `gh api --allow-escape-sequences repos/<repo>/actions/jobs/<id>/logs`.
- "Acceptance baseline regression" comes from `ci_mode.py` in foragent-acceptance and compares with current main, so a later merge reads as lost tests (#1581). `test_overlap_facts`-style comparisons flake on live load.
- Red shared with main: bisect main CI by time with no `src` diff; a fixed-clock fixture beside a wall-clock hook is typical (`test_presence`, `test_cli`). Such failures return despite a REBUTTAL; fix small choice-free fixtures in the branch.
- Isolation leaks read as product failures: threaded restore of `processes._process_paused` (#1141), an import-time alias of a patched `commands.repo_store` (#1182).
- Not issue defects: many `Runner exited: 143` or "Crash recovery" at once (10-31 03:08–03:23), GitHub transport errors then `Re-run review UNUSABLE` (11-02 04:29–04:40), held on "Unusable acceptance shard/artifacts" (#993, #1560; both merged later).

## Merge path

- `orch._protected_broken`: protected ids from `_capture_baseline(..., required_ids=frozenset())` minus `_baseline_exclusions` read just before use; runs `baseline.command` on existing *files* (one removed node id voids a selection) in head+base merged; unreadable report = all broken; capture failure = tests fail. Never replay the user's command on base or put head content there. Steps after the suite loop honour `refused`; test with `suite_case` (`merge_case` is non-pytest).
- Same-base caches with wider exclusions are reused on purpose (spec answer, pinned). `pending_acceptance_exclusions` takes base and head; capture keys stay base-only (#1341).
- `orch.merge`: `merge_turn.claim` rejects a live holder before `merge_lock`; `_premerge` to `merge_pr(checked.head)` in one lock; failure withdraws only its own reservation (#1379). `run_checks_approved.py` holds approved-input reads; person path uses `absent_ok=False` because approvals can store `ABSENT_BLOB` (#1559).
- Slots on the merge path ([#712](project_712_merge_turn.md)): `run_waits._set_phase` gates every return from an idle phase; `admit_run(owner=)` asks `holds_run_turn`; nonholders wait in `run-slot-wait` for `min(600, _remaining)` then `_leave_without_slot`.
- Group merge: `run_group_check.check()` reads its own baseline (#1139); `judge()` is the single verdict; `void_reason()` runs before base refresh; `record_merge` proves provenance under the lock (#1140); both base-move points share `reuse(target)` with `neutral_rule`, leaders too (#1141). `test_merge_turn` pins `merge_group_max` to 1.
- Stale saved branches: recovery in `runner._execute` after `add_worktree`; own locked edits from `rev-list HEAD ^origin/<target> ^origin/<base> -- <locks>`; keep the merge via `Run.merge_conflicts` (#1345).
- `verify()`: Done when, baseline, related tests by AST; an empty pytest selection is skipped since it runs everything. PR bodies are capped by `run_pr_body.LIMIT`. `orch_reinstall.after_merge` installs from a fetched detached SHA and only records failures.
- Worker slots ([#106](project_106_capacity.md), [#243](project_243_test_slots.md)): `run_issue` holds `hold_workload(RUN_KIND)` before `update_run(pid=getpid)`; `_store_scan` reuses only a live holder. Holds that queue behind valid work wait as long as that work. `run_check_slot.StopHookBudget` reads the hook timeout from `claude-settings.json`. Panel size is the number of perspectives.
- Notes: [#376](project_376_base_move.md), [#483](project_483_baseline_follows_base.md), [#123](project_123_baseline_survivors.md), [#433](project_433_baseline_facts.md), [#567](project_567_ci_exclusions_drift.md), [#368](project_368_ci_not_started.md), [#450](project_450_pr_title.md), [#466](project_466_harness_guard.md).

## Gate, scheduler, state

- Facades (`scheduler`, `runner`, `run_publish`, `state`, `cli`): split modules call `facade.X` at call time and locked `*_split` tests require old names ([#196](project_196_state_split.md), [#560](project_560_run_publish_split.md)).
- GitHub limits: `github-pause.json` per project, later time wins; `tick()` checks first; `is_rate_limit` excludes plain 403. Executor limits: `pause_executor`, `_lane_held`, `_available_executor` rechecks. Notes: [#107](project_107_claude_quota.md), [#318](project_318_usage_recheck.md), [#786](project_786_executors.md), [#186](project_186_executor_choice.md), [#238](project_238_tiers.md).
- Records: `records.load_record`; load→save keeps unknown fields (`_write_run`, `write_back`). `run_parent.parent_context` is the single contract writer; `carried_contract` carries it only for the same body sha and approval time (#1184). `add_waited` increments, never snapshots.
- Stop: `fa orch stop` accepts `scheduler_stop.unfinished_runs`; `run_is_live()` serves park and the status row only; `unsafe_target()` refuses reused PIDs; running records stay on every screen ([#370](project_370_orch_stop.md)). Recovery with an open PR blocks rather than restores ready. `start_run` takes `worktree_lock`.
- Blocked: `reasons.current_blocker` computes from labels and the latest `(created_at, id)` marker plus outbox FIFO. `save_flag` never guesses `local_only`; history readers use `load_local_flag`. Raw rows need `is_pull_request`, state and label checks (#1182).
- Park: intent is the GitHub comment `<!-- foragent:orch kind=parked|unparked -->`, newest wins, journal is audit only. Park removes `approved` and writes `## Waiting`, so re-approval may need a retry (#1183, #996).
- Waiting: `order_waiting.section()` is the public reader; the inbox request carrying `wait_resume_key` is the only receipt, read in the tick lock. Claim files lose work on a failed write (#1181, #157).
- Advice: `run_previous.add_hint` appends, `consume_hint` removes the delivered prefix. `orch_advice.publish` precedes the hint or delivery and falls back to the outbox, including manual-command handoffs. Event summaries collapse whitespace; comments carry advice verbatim (#1185).
- Flow: `FlowRecordsMixin` uses leaf `flow.lock`; `record_flow` swallows Exception, so bugs look like no rows; read the clock inside the lock (#1581). `_record_issue_states` is the only `issue-state` writer; take `history_baseline` before `_observe` mutates; edges use `edges_at {at, revision}` (#1578). `scheduler_flow` keeps decision figures; release needs a whole good pass (#1579). Setups that ingest also write `issue-state`.
- Scorecard reverts read `origin/<base>` on each read; shallow is unknown; "Reverts owner/repo#N" is not counted (#1560).
- Checks for every tick exit go in `orch_spawn.deciding()` or `after_tick`. Git in the tick needs a "has targets" guard for `test_cli`'s fake. Notes: [#995](project_995_tick_admission.md), [#603](project_603_cross_lane.md), [#287](project_287_plan.md), [#1027](project_1027_file_overlap.md), [#1042](project_1042_assign_goal.md), [#129](project_129_blocked_push.md), [#952](project_952_silent_push.md), [#108](project_108_say.md), [#415](project_415_sleep.md), [#650](project_650_wake_grace.md), [#413](project_413_refresh.md), [#365](project_365_machine_yml.md), [#366](project_366_installed_gates.md), [#417](project_417_update_check.md), [#497](project_497_memory_critical.md), [#564](project_564_step_times.md), [#416](project_416_retry_fixes.md), [#420](project_420_whole_picture.md), [#379](project_379_risk_map.md), [#104](project_104_skill_catalog.md), [#449](project_449_overlap_facts.md).

## Runner prompts

- Hints go in before the `/goal` wrap and are consumed only after a normal result. Rework appends `SEARCHED_LIST_INSTRUCTIONS` after two locked substrings. Lanes reach the PR body only through `.foragent/` files.
- Pass the existing `GitRepo` on: `local_skill_paths` lives on the instance. `fa doctor --live` runs the real runner path with `FakeTracker`.

## Neighbours (D107 moved these modules to review-engine and roles)

- Review: `SPEC-CONFLICT:` stops a run through `_stop_on_spec_conflict`. Facts read verdicts from `kind=review` events and findings via `code_review_findings()`.
- Roles: `orch_role_prompt` and the skill both state memory rules, change both (#1188). Task measurements go on requests or issues, lessons in memory. Inbox validation precedes writes; closing verbs use `set_request_status(check=)`.
