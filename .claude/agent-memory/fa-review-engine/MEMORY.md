- Older lessons for this area are in ../fa-orch/MEMORY.md (lane split from orch on 2026-10-31); the orch dream drops lines that moved here.

## Detail notes
- [#226 NOTE lines](project_226_review_notes.md): review_notes.py, every implementer feedback channel, persist side effects when the text exists.
- [#1016 WHAT/HOW](project_1016_review_what_how.md): frozen review text in test_risk_map and runner_support; the four reviewer-input paths including pr-body.
- [#514 base-move](project_514_neutral_more.md): neutral rules; treat .foragent.yml as untrusted.
- [#533 panel](project_533_review_panel.md): log paths, merge-conflict recipe and test imports still hold. Its branch condition, `shares(config, effective)` signature, cap and limit order are outdated; use "Panel and perspectives" below.

## Panel and perspectives
- Risk-map first round = one whole-change reviewer per perspective: `review_perspectives.shares(config)` gives `[[item], ...]`. Defaults are 4 (goal-fidelity, concurrency, interruption-rollback, test-quality). `orch_reviewers.effective` is only the pool `max_workers` cap (`_panel_review(workers=...)`), never the reviewer count.
- `_panel_review` joins every reviewer before one merge call and returns only the merger's parsed result. Raw verdicts live in `review-<round>-<k>.txt`. Grouping by defect class is purely `PANEL_MERGE_PROMPT` text in run_prompts.py.
- An UNUSABLE panel still saves `review-K.txt` with raw member VERDICT/FAIL text. Never treat that file alone as a merged result: read the review journal (`verdict` or, on SPEC-CONFLICT, `review_verdict`).
- Panel outcome order after all members finish: interrupts first, then a completed spec conflict, then a limit (requeues the shared issue once, from the aggregate), then other errors become UNUSABLE. Members only record quota and hold facts (`requeue=False`).
- Limit fakes: search every `_limit_wait` test double when its signature changes (a lambda without `requeue` failed CI in #1451). Order concurrent responses by `review-<round>-<member>.log` identity, with executor barriers plus wrapper-completion events.
- Panel fakes: `PanelExecutor` treats calls after its first perspective batch as merger calls, so size messages to `len(CHECKLIST)`, not the reviewer count. A test with a missing member verdict needs `max_retries=0`, or a second implementation round reuses the fake wrongly.
- With a bounded pool, reviewers queued after a limit may never reach the executor (`_available_executor` sees the limit first). Assert `merges == []` and `reviews <= perspectives`, not an exact count.
- `PanelExecutor.maximum` proves nothing when overlap=1. Count your own busy/peak around a short sleep, then break it with `max_workers=len(prompts)`.

## Reviewer slots and machine budget
- Every panel reviewer holds `capacity.hold_workload(kind="review")` unconditionally, for every store and even a serial panel. The pool is `min(workers, perspectives, workload_budget)`. Gating on `store.root.parent == machine_root()` or `at_once == 1` is an admission bypass (#1169 review; settled as option 1 of its question). Locked test_review_parallel therefore writes shared budget 9.
- Admission waits use the run's remaining time (`runner._remaining`), not the helper's 600 s default: a short wait made budget-1 serial reviews UNUSABLE (#993 review-4).
- A record's status does not prove a live reservation. Decide reuse from a live PID or a reservation before claiming the PID; never claim first (#993 review-6). `capacity.RUN_KIND` is the direct runner's parent slot, counted first by `_store_scan` so the first reviewer reuses it.
- A direct rerun of a finished record must call `StateStore.start_run` after admission, then close the temporary hold. `update_run` alone leaves it finished, and merge queue `_live` then times out after review PASS.
- `start_run(pid=...)` already stores `observe_tree(roots=(pid,))` as `process_identities`. No caller argument is missing; do not observe twice (#993 review-10 misread this). tests/conftest.py's default process table is empty, so use `fake_process_table` to test it.
- Dream runs are in `active_runs`; `by_pid` reuse makes run plus workload one slot. `tests/test_cli.py` `dream_workload_reuses` covers reserved/dead PID × normal/interrupted.
- `hold_workload(now=...)` defaults to the wall clock; only `runner.run_issue` and `review_only` pass `ctx.now`. Forcing `store._now()` for everyone took slots from callers that never wrote the fixture reservation (lost test_order_perspectives at budget 7). The proof is `test_worker_budget_unit.py::test_runner_entries_admit_their_reservation_by_the_context_clock`, through real entries. Before widening a shared-clock fix, list every `hold_workload` caller and run their acceptance files.
- Capacity defects in #993 appeared in combinations each passing test covered separately (direct rerun × panel threads, budget 1 × long reviewer). Test the matrix: entry × budget × liveness × normal/exception. tests/test_review_capacity.py is the model.
- `capacity._alive` treats `os.getpid()` as alive without signalling. The `_ADMISSION` thread lock stops sibling reviewers losing the non-blocking flock to each other.

## Verdicts and retries
- Only explicit FAIL spends `review_failures` or joins rebuttal disputes. Missing-verdict text still goes verbatim to the implementer (D44). Empty successful replies stay journaled UNUSABLE with raw evidence and zero findings, and never set `last_review_pass_at`.
- Keep executor errors and timeouts (`execution_failed=True`, bounded by `review_max`, flag RETRIES_EXCEEDED) apart from replies with no verdict. Unstructured UNUSABLE findings set `attempt.failure` and are bounded by the caller: `runner._implement_and_publish` `run.attempts < max_retries + 1`. Do not add another classifier or threshold; reviewers keep raising this because they cannot see the caller (#1324 reviews 1 and 5).
- Native Claude uses /code-review with an empty prompt, so its verdict contract must reach both CLAUDE.local.md and the PR-body `review_scope_rules`. Native scope fixtures need an explicit verdict even for `[]`, including SPEC-CONFLICT fixtures expecting FAIL.
- `parse_code_review` enforces exactly one verdict; `code_review_findings` is the format converter also used by facts. `orch_facts_reviews.round_classes` honours journaled verdicts first, and unjournaled old artifacts fall back to `code_review_findings`, not the strict parser.
- When moving a constructor, carry `execution_failed=True` with it: merges lost it once. To prove a lost ceiling, supply a third implementation and PASS after FAIL/error; the mutant must merge and the real code must refuse.

## Later rounds (confirmation)
- `_review` calls `review_single.first_review_findings` before risk-map fanout, so later rounds skip the panel. It scans numeric rounds in order (not `review-K-member.txt`), skips journaled UNUSABLE, invalid and finding-free rounds, and keeps the first usable defect review verbatim as the anchor for all later rounds. Reading only the lowest file was wrong (#1170 review-2).
- Legacy native first rounds may lack VERDICT, so `code_review_findings` also recognises them. Confirmation on Claude uses the prompt transport (no `review_level`/`review_pr`, parse VERDICT), including pr-body mode.
- Test transports must tell /code-review from a prompt call and return the matching envelope (`{"type":"result","result":"VERDICT: PASS"}` for the prompt). Do not loosen the production parser for a fixture.
- `next_review_round` and `_saved_rounds` live in review_single, re-exported as `run_review._next_review_round` for the runner.

## Publishing path shared with review
- Review-only entry and FIFO merge-turn share `run_publish._refresh_reviewed_head`: review a behind head, merge the captured base, run guarded Done when only on the clean merged head, then lease-push. Real conflicts quit and keep base-changed/RISK_HIGH. Keep exact-head checks before refresh so remote changes during review are not overwritten.
- test_run_publish parallel-lanes shares one fake clock: a FIFO owner stays live until `finish_run`, so a follower's fake sleep can exhaust 90 minutes during real Git cleanup. Coordinate `finish_run` and the follower with Events.

## Tests and pins
- `goal_condition` wording is pinned in tests/test_run_prompts.py, tests/test_run_execution.py and quoted in docs/impl/p7c-native.md. Change all three together.
- When a fixture fixes `now`, pin every clock the code reads (test_presence pins `statusline.datetime`; an unpinned read broke every PR when the 24 h window expired). Live `os.getloadavg()` makes facts repeat-read tests flake about 1 in 10 under load.
- Prove a flake or a CI-only loss with a throwaway `-p` plugin (`PYTHONPATH=.foragent/x`), pinning `os.getloadavg`, `os.cpu_count` or `capacity.cpu_busy`. test_scorecard_records failed only with 4 CPUs because load per CPU crossed OVERLOAD 1.5. Rebut rather than edit a locked fixture.
- Mutation proof: use a temporary source copy with PYTHONPATH, confirm the copy is really imported, and run sequentially. Probes in test files change only copied functions or monkeypatched attributes, never tracked source. Make a mutant fail on the wrong count, not on fixture exhaustion. For long waits use `wait_s=0`.
- After merges, compare top-level ImportFrom `(module, name, asname)` by AST for duplicates (#993 left capacity imported twice). For an EOF conflict in tests, keep both additions and AST-compare with index stages 2/3.

## CI and environment
- Before fixing a red PR, compare main. `gh run list --branch main` first; main-wide breaks are not your diff and another lane may have a fix in flight. On conflict take main's side and diff against `git show <main-sha>:<path>`.
- Collect failures come from the PR merge ref, so a file on main can break collect while HEAD lacks it. Check `gh run view <id> --json jobs`. Do not create another order's deliverable to go green.
- Empty CI text: read with `gh api --allow-escape-sequences repos/<owner>/<repo>/actions/jobs/<job>/logs | sed 's/\x1b\[[0-9;]*m//g' | grep FAILED` (`gh run view --log` refuses while running), or GitHub `fetch_workflow_job_logs` when local gh cannot connect. Without saved CI logs, label the cause as inference.
- The acceptance CI workflow installs `foragent @ git+…main`, so its green says nothing about PR code. `missing acceptance shards: observed []` is missing comparison evidence, not a slot defect.
- Baseline comparison: `git archive origin/main src/foragent` plus `PYTHONPATH=<copy>/src`. The shell is zsh: use an array `F=(a b)` and read pytest's tail, because "no tests ran" looks like zero differences.
- A workspace-only sandbox can block `fa test` at test-slots.lock and git at index.lock or ORIG_HEAD.lock (seen in #1168, #1170, #1324, #1424, #1451). Record verification and mutation proof as not run, and leave exact commands in TRY.md. Resolved file contents are not a resolved index. `git checkout` is denied: edit markers out.
- `.foragent/` disappears when the worktree is recreated; restore SEARCHED, REBUTTAL and TRY from real re-verification. Check ancestry before rebasing a recreated worktree.
- A protected-baseline timeout is not an assertion failure; never narrow the protected set. `run_checks._check_protected` now owns the measured limit and retry (#1347).
