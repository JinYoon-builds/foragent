# Live Orca handle and silent-orchestrator push (r33, r38)

## Incidents and root causes (2026-10-29/30)

- r33: a reboot changed Orca terminal handles. The state directory's watch-terminal.json still contained the previous handle because watch_open._launch wrote it only when opening a view; the planning session's SessionStart path never recorded its current handle. The existing reader rejected the stale terminal, and the lane view fell back to Apple Terminal.app through osascript. Repair the missing startup write and keep existing reader/screen-selection behavior.
- r38: a gate-spawned `claude --bg` orchestrator waited about 15 hours on a question nobody could see. orch_spawn._note_silent wrote hourly orch-silent events but never called the notify path. Repair that notification gap and give the orchestrator skill the visible question channel, `fa orch ask`.

## Decisions

Two independent unattended size-s issues, no dependencies: view/orca-handle and orch/orch-silent-person. Features are implemented later; this order contains the plan and locked acceptance files only. Tests stay offline and reuse the repository's existing entry points, fake Git history, process table, fixed clocks, and notify.send recording. No shell/LLM meaning classifier or imposed agent step order is part of this work.

### Orca handle ownership and screen compatibility

The installed hook is `fa statusline --session-start`, installed by src/foragent/statusline.py and dispatched by src/foragent/commands/statusline.py to statusline.session_start -> presence.session_start_message. The hook records the current person's {executable, handle, recorded_at} in the project StateStore's watch-terminal.json. Missing context and write failures preserve today's hook output and success behavior.

The ownership target is one writer: the hook records the person's current terminal. The specification explicitly allows retaining _launch's existing foreground write when a locked acceptance test requires it. That exception is necessary here: test_recorded_orca_terminal_is_probed_before_split_or_new_window (recorded-alive and recorded-dead, test_view_open.py) and test_orca_wins_then_a_dead_recorded_terminal_falls_back_to_tmux (test_view_open_terminals.py) first open the view with an environment handle, then expect a recorded-terminal probe without it. Retain that write and explain the exception in the PR. Do not rewrite or weaken those locked tests.

“Beside term_new” preserves D76's existing recorded-terminal route: show/probe term_new, then create an unfocused orchestrator tab for the same worktree. Only the lane view splits beside the planning session. This order does not add a --terminal option to terminal create or change screen-selection policy.

### Silence facts, notification and settings

Use the existing live-session and heartbeat facts. The last heartbeat write is the silence origin; use recorded spawn time only when no heartbeat exists. If neither timestamp exists, keep the diagnostic without inventing a silence age. Alert at >= orch_silent_alert_minutes, default 30; 0 disables this push. Preserve hourly diagnostics and current spawn behavior. Persist one-alert-per-episode state across reopening and rearm only when the gate sees a fresh heartbeat.

Register the setting in src/foragent/settings.py as well as Config/validation: that SETTINGS tuple, not Config's fields, drives fa settings list and set. The new messages/orch_silent.py module supplies both languages, including the settings description. Stable push phrases are “오케스트레이터가 조용해요” and “The orchestrator is running but silent”; the body states that the session may be waiting for an unseen question and points to `claude agents` and `orch: <repo-directory-name>`. The name uses root.name, consistent with the existing spawn title.

A session that may be invisible asks through `fa orch ask` (which pushes) rather than AskUserQuestion alone. Do not inspect Claude agents state or answer for the orchestrator.

## Acceptance and compatibility evidence at preparation

The source checkout was clean and HEAD equaled main at 9e92c9aa4af5fa17dfecfd8bf97f5f63ba0d4bdb. New tests are staged only under order-out/tests/acceptance; their eventual repository paths and full contents are in each issue's acceptance_files. While staged outside tests/, run pytest with the existing tests/conftest.py loaded as `-p conftest` and PYTHONPATH including src, tests and tests/acceptance. After normal publication places them in tests/acceptance, the Done when commands need no such plugin argument.

New modules were run separately against that unchanged main source. The handle module produced 1 failed, 3 passed: `AssertionError: SessionStart must replace the stale Orca handle`, with `term_old` still present. The silence module produced 4 failed, 1 passed: Korean and English episode cases had zero pushes at 31 minutes; the no-heartbeat case had zero pushes 31 minutes after recorded spawn; the settings case found no orch_silent_alert_minutes registration. These are missing feature assertions, not collection/import failures.

The required existing command `uv run pytest tests/acceptance/test_orch_spawn.py tests/acceptance/test_orch_spawn_liveness.py -q` produced 2 failed, 7 passed before any implementation. The existing failures are test_orch_spawn_background_cooldown_survives_store_reopening (missing --session-id) and test_orch_spawn_liveness_reused_pid_spawns_and_records_the_new_identity (no launched command with --session-id). These pre-existing #413/session-identity failures are not caused by these Goals and are not silently waived or made part of this order's implementation.

The three recorded-view compatibility cases named above were also executed and passed. Strict removal of _launch's writer would contradict them; this plan uses the specification's compatibility exception. The orch liveness no-push expectations are at 5/15 minutes, below 30, and therefore do not conflict. Preserve every other existing locked expectation, including presence output and switch behavior.

## Implementer discretion

Any detail not fixed here or in the acceptance tests is the implementer's choice: take the smallest change that keeps the listed tests and today's behaviour elsewhere, use the messages registry for new human text (English and Korean), and state each choice in the PR.
