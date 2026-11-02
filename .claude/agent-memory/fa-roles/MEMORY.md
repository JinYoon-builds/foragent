# roles lane memory

- Older lessons for this area are in ../fa-orch/MEMORY.md (lane split from orch on 2026-10-31); the orch dream drops lines that moved here.
- Per-issue history, older than this file, so check names against the code: [dreams #189](project_189_lane_dream.md), [facts #333](project_333_facts.md), [resource facts #541](project_541_resource_facts.md), [scorecard flow #779](project_779_scorecard_flow.md), [ask/planning #970](project_970_ask_planning.md), [spawn #128](project_128_orch_spawn.md), [liveness #590](project_590_orch_liveness.md), [role agents #716](project_716_role_agents.md).

## Defect classes reviews keep finding in this lane

- Delivery order does not say what a completion answers. #1231 (an old dream's `dream-done` after a redo), #997 (one cutoff consuming another order's turn) and #1155 (a tick between the status change and the answer event) all failed this way. Compare recorded times or named coverage, never journal order or one global timestamp.
- Never decide from `orch-events.jsonl` alone: `trim_journals` drops lines after 30 days, and events are appended after the locked status change. Put the deciding field on the record inside the same lock. Status and audit writes can fail independently after the commit.
- A fingerprint or dependency holds only the signal's own facts. Giving every kind the binding re-raised unrelated not-now signals on a cap→quota change (#1155).
- A resource moved between records must be checked at each record change, since a scan after the call cannot see the window (#994: two holders, then zero).
- When a fix lands, sweep its whole class: callers of the shared writer plus prose in skills and references. #1233 and #1336 each came back for a missed path.

## Role signals (`orch_role_raise`, `orch_roles`, `orch_facts_kinds`)

- A raise stores `facts` (`orch_role_raise.fingerprint`) on the inbox request, with no side file, so it survives a store reopen. Comparison goes through `evidence`, against the last raise whatever its answer was (called, not-now or none).
- Only new members of `ENTITY_FACTS` sets, a `VALUE_FACTS` change or a `SHARED_FACTS` change re-arm a signal. Shrinking sets and count drift stay quiet. A cleared signal still re-arms. The cooldown applies only to legacy requests without `facts`.
- Only `idle_lanes_with_backlog` has a shared fact (`binding`).
- Unit fixtures: `orch_roles.observe` returns 4 values (files, kinds, numbers, binding), so monkeypatches must too. A raise→not-now loop across days must add a real entity per day, not change an arbitrary `n`, or the rule keeps it quiet.

## Inbox and role decisions (`orch_inbox`, `orch_role_decisions`)

- `orch_inbox.set_request_status` is the only writer of request status. Its `require_answer` is opt-in, so a new internal caller can close with no answer (`orch_tick_outcome.record_success` did). Sweep its callers and the close lines in skills/fa-orchestrator and skills/fa-order.
- Proposal completion writes role, accepted, answer and `decided_at` with the status, in the check hook under the state lock. `orch_role_decisions.recorded` projects those fields over audit events, for the scorecard and role-removal evidence. Later status notes must not move a decision between day buckets. Legacy proposal events stay readable with no backfill.
- `request-status ID done` without `--answer` stays legal because locked inbox, tidy, refresh and lane-view checks need it. It reuses a stored nonblank answer, else sets `empty_answer_reason`. Explicit blank `--answer` is refused. Compare an omitted input and a blank input separately.
- `request-status` refuses proposal completion and points to `role-decision`; role-signal answers belong to `signal-answer`. CLI role requests get role identity from configured `--from` names.
- `add_request` returns the record with `seq`, and `requests()` strips it via `_public`. Rejection tests compare request-file and journal bytes before and after.
- Keep `--note "<issues>"` and add `--answer` on the same `request-status` command: test_orch_order_handoff follows the [order card] pointer to references/order-card.md and needs that prefix.
- `write_note`: the `note` event is the last write under the lock, so a failed projection leaves no event. Validate `--now` before `person_found`, or a refused command leaves a person-found line.

## Dream redo (`orch_dream`)

- A redo is derived, not stored. `fa orch dream --redo <lane>` only appends a `dream-redo` event, stamped with `store._now()` on the same clock as `record_done`, and kicks the tick. It starts no worker and closes no PR, because a worker of unknown liveness can publish after any lookup.
- `dream_due` keeps the lane due until a dream's `covered_until` (prompt build time) is later than the redo. `orch_dream.start` is the single closer through `stale_for_redo`, and `fa orch merge` refuses the same PRs. `awaiting_pr` filters to DONE/PUBLISHED so a redo never hides a published PR.

## Role sessions and schedule (`orch_architect`, `orch_role_schedule`)

- `orch_architect.call` runs its `hold_workload` inside `contextvars.Context().run`, because a role is a new worker and an outer hold of the same thread would be reused with no admission.
- `_pass_slot` rewrites the held record to the child pid in place, then renames it away from the path the hold removes, under `capacity.machine_lock`. `<role>.json` names it in `workload`. `run_session` removes it only when `started_at` matches. The next `call` removes a crash leftover, because capacity liveness is signal-0 only.
- If a post-launch record write fails, `call` stops the child through `Popen` (`kill`, then `wait`: a zombie still passes signal-0). Test that with the fake process table only.
- `load_roles` derives orchestrator, architect and docs when `config.roles` is empty. An automatic start must also check `role in config.roles`, or every locked tick test launches a real architect. A manual `fa orch architect` keeps derived entries.
- `orch_role_schedule.observe` is the only writer of `order-finished`; it freezes the finish time once. GitHub `updated_at` moves on any later comment or label, and only `row["observed"]` from a full listing holds GitHub's value. Orders older than the 30-day journal are skipped, so trimming cannot re-record them.
- Between 5-minute listings a tick sees closed issues but no new open order. A closed child with a `foragent:plan-child` marker and an unknown order id gets one `tracker.parent_issue` read.
- Cutoffs: `dream_due` compares an order's finish with the lane's last dream and counts that order's merged children whenever they merged. A marked note covers only the orders named by the retrospective requests posted since the previous note; only a note with no such request covers by time. Sessions keep a time cutoff, by the spec.

## Facts (`orch_facts*`)

- Use `from foragent import scheduler`, then `scheduler.X`. `from foragent.scheduler_lanes import X` passes in a pytest session that already imported the scheduler but fails in the real `fa orch facts` with a circular import. Prove it with a fresh subprocess or the real command.
- `freeze_merge_store` copies other stores' ETA inputs into `merge-stores/`. Pass frozen StateStores to `progress.merged_samples`; `progress.machine_stores` reads live paths and breaks the one frozen read.
- `_machine_runs` counts workloads with `orch_facts_machine.workload_slots`, which mirrors `capacity._store_scan`'s reuse rule. Change both together. `orch_binding.from_tick` passes an empty alive map, so there every workload counts as live.
- Scope drift is published raw, and the runner's `changed_files` includes lane memory. Do not filter it without an order. Count all rows before you name a cause: on 2026-11-02 only 87 of 653 drifted executions were memory-only, though the first examples all looked that way.
- Facts acceptance fixtures must record a gate resource sample in the isolated machine root. Without one, `freeze_sample` reads live load, so repeated observations differ. Keep the live fallback for unrecorded machines.
- The human `fa orch facts` output is locked to five blank-line blocks and at most 30 lines, so new lines join the last block.

## Text and generated files

- skills/fa-orchestrator/SKILL.md is at its 150-line cap (test_orch_order_handoff, counted by splitlines). Append to an existing line or move detail to references/. Check with `python3 -c "print(len(open(p).read().splitlines()))"`, since `wc -l` is one off without a trailing newline.
- Role agent text has one shared `GITHUB_WRITES` paragraph in `role_agents.py`. After changing `agent_text`, regenerate `.claude/agents/fa-*.md` with `uv run python -c` (system python lacks yaml). Guidance never shows `gh issue|pr ... create/edit/close` lines, even as counter-examples: a locked regex flags them. Route recovery through `fa orch dream --redo <lane>` and `fa orch note --why`.
- A new `settings.SETTINGS` key needs `settings_desc_<key>` and `settings_window_label_<key>` messages, or the settings window raises KeyError. tests/test_settings.py and tests/test_watch_settings.py pin the key list and line count.
- A free-text field on an orch event must survive `str.splitlines`. `orch_events._write_line` escapes U+2028, U+2029 and U+0085 for this; `scheduler_stop` and `orch_role_schedule._events` read journals line by line.

## Tests

- A test that starts a role must fix the load and memory sensors: add its module to `ROLE_START_MODULES` in tests/conftest.py. Measure the list by forcing `_sensor_block` to "overload" and comparing failures.
- A change to tests/conftest.py outside `## Files` held both #994 and #997 for a person. The fix came back as an order edit listing the file, so expect it when a role change adds a sensor read. An order edit can also change other decisions, so re-read the whole Goal on the retry.
- A fixture with a fixed `now` must pin every wall-clock reader on its path. #1155 and #1233 both hit CI failures from `datetime.now` against a fixture clock, at certain hours or 24 h later. When CI fails in an unrelated test, check this first.
- `tests/acceptance/test_lane_dream.py`'s gate_respects_capacity needs a budget of 2 but does not fix the sensor, and `capacity.next_parallel` halves the budget at 1.5 load per CPU. It fails with `assert (1 == 2)` under forced load, on origin/main too. Natural failures took 11-49 s against the usual 3 s, but 141 traced runs at load 6-10 never caught one, so call load the likely cause, not the proven one.
- A collect-job failure naming a file missing from main can be main's fault. Check `git merge-base --is-ancestor <fix> HEAD` and `git rev-list --left-right --count origin/main...HEAD` first.
- A Done when that needs a sibling issue's feature cannot pass alone. Compare failure messages with the sibling's goal, then write .foragent/QUESTION.md. On #994 that came back as an order edit on the next attempt.

## Commands and verification

- Break-it runs: stay inside the repository, since a goal may forbid edits outside it and /tmp backups count. Copy the file under `.foragent/` (mkdir first, it may be gone after a merge retry), check `cp` succeeded, and restore in `finally`. `git restore` is fine only on a clean tree; on a retry it discards uncommitted fixes.
- Under `pytest -x`, a permanently failing test makes every mutant look caught: three of thirteen were really missed. Check that the failing name belongs to the mutated behaviour.
- Do not edit `capacity.py` to force a sensor: `fa test` loads the same module and waits for its slot under a forced critical reading, which hung past the tool limit and left the edit in place. Force it inside the pytest process from tests/conftest.py with try/finally. Let `fa test` wait for its slot: 170 s timed out on a busy machine, 540 s was enough.
- Base comparison: `git archive HEAD src tests pyproject.toml skills docs | tar -x -C .foragent/base`, then run `<worktree>/.venv/bin/python -m pytest` with `cwd` and `PYTHONPATH` set to the copy, and print `capacity.__file__`. `fa test` from a copy does not load that copy's conftest, so it proves nothing. Delete the copy afterwards. Failure lists on base go stale, so redo the comparison rather than trust an old count.
- `.foragent/` is wiped between attempts and is not always git-ignored (`git status` lists it on some retried branches). Rewrite TRY.md and SEARCHED.md and re-run break-it, and delete scratch copies before you finish.
- CLI trials without touching a real inbox: from a temp git repo with its own `HOME` (state root `$HOME/.local/share/foragent/<owner>__<repo>`), a copied `.foragent.yml`, `PYTHONPATH` set, and `.venv/bin/fa`. Under zsh a command held in a variable fails, since zsh does not split words. A real-process check can use a fake `claude` on PATH.
- Evidence is split: `runs/issue-N/check-done.log` records Done-when exits apart from baseline logs. A baseline log that stops mid-run with no traceback shows an incomplete run, not a culprit. Check timeouts separately from assertion failures; #1233, #1336 and #1339 each retried on baseline timeouts with `Check timed out.` lines in their logs. The limit is now adaptive (`run_baseline_limits`, #1347).
- Three other pre-pytest blocks look like failures: a sandbox denial on `test-slots.lock`, one on the shared `.git` rebase directory, and a denied `git rebase` that prints exit 0, so inspect its error and the resulting HEAD. Record these without bypassing the slot. A break-it that stops on the lock is not mutation evidence.
- After a retry, check ancestry before rebasing: the runner may already have placed the commit atop origin/main, or merged the reviewed work with the new gate main (inspect both parents and `HEAD...origin/main`).
- #1337's four hash-mismatch stops were not test failures. The approval snapshot had read older local bytes of the corrected locked tests, so re-approval only helped after the checkout was synced to main.
