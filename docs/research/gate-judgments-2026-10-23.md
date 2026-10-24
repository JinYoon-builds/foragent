# Judgments hard-coded in the gate, scheduler and runner (2026-10-23)

Inventory for D80 (the orchestrator judges, the gate executes). S = safety line, stays deterministic in the gate. P = policy, should become a plan knob the orchestrator sets. D = default, may stay in code but should be overridable by the plan. Line numbers are from main at 0b0d64f.

## Selection, order, lanes, parallelism
| Where | Rule today | Source | Class |
|---|---|---|---|
| scheduler.py:529, state.py:118 | Within a lane, the lowest ready issue number starts first | literal | P — the orchestrator faked order with blocked_by links; 30 orch issues waited 8 h behind #138 |
| scheduler.py:657, labels.py:32 `lane_order` | Lanes are served alphabetically, `main` last | literal | P |
| scheduler.py:661, 713 | One run per lane, one start per lane per tick | literal | D (lane width; #145 clones) |
| scheduler.py:576-589 | Every approved issue that passes checks becomes `ready` | literal | P |
| scheduler.py:417 | Open blocked_by keeps an issue approved | literal | S |
| scheduler.py:406 | A blocker closed as not_planned blocks with spec-question | literal | S |
| state.py:510-522, runner.py:877 | Lanes pause only on usage limits (`limit_wait_minutes`); no resume tool | config | P — the orchestrator called `pause_lane` from Python, no record |
| capacity.py `next_parallel` | Machine-wide AIMD (start 2, +1 below load 1.0/CPU after 2 min, halve on pressure or 1.5/CPU) | literal | S |
| scheduler.py:638 | Per-repository ceiling | config `max_parallel` | P — edited by hand today |
| scheduler.py:596 | No new runs outside `run_hours` | config | D |
| launchd.py:86, state.py:226 | Tick every 300 s; issue refresh every 5 min | literal | D |

## Retry and attempt budgets
| Where | Rule today | Source | Class |
|---|---|---|---|
| runner.py:1795 | Check or review failure retries while attempts < max_retries+1 (3 total) | config `max_retries` | P |
| runner.py:698 | CI failures share that budget | config | P |
| runner.py:1769 | Third review FAIL holds | config `review_max` | P |
| runner.py:1490, 1371, 1388 | Conflict retries, then one rebuild on the latest target, then block | config `conflict_retries` + literal | P |
| runner.py:1535 | Base moved three more times: timeout | literal | P |
| runner.py:854 | Zero-turn executor result blocks without retry | literal | P |
| runner.py:884-894 | Two transient errors switch to the fallback model; backoff 120·2^n s up to 960 s; total wait cap | config + literal | wait cap S, switch point P |
| runner.py:602, 208 | Stop hook blocks up to 8 times; Codex goal says stop after 40 turns | literal | D |

## Stall, crash, timeout
| Where | Rule today | Source | Class |
|---|---|---|---|
| scheduler.py:348-355, 311-322 | No progress for `stall_minutes`: kill; first stall back to ready, second blocks | config + literal | kill S, restart count and target state P |
| scheduler.py:236-257 | Crash returns to ready; two crashes in a row block | literal | cleanup S, count P |
| scheduler.py:229, 305 | Crash or stall with an open PR blocks | literal | D |
| state.py:433-434 | Restarts carry over; attempts reset to 1 except after limit-wait (tier and budget restart) | literal | P |
| runner.py:672-682, scheduler.py:344 | Run budget `max_runtime_minutes` (90); gate kills at +10 min; "Too big for one session" | config + literal | hard kill S, budget size P |
| runner.py:1408, 1412, 518, 610 | 900 s limit per Done-when, baseline and hook check | literal | D |
| runner.py:1423-1431 | Related unit tests: max(900, remaining) s, timeout blocks | literal | P |
| runner.py:1614-1632, 1956 | CI polled for 20 min | literal | D |
| scheduler.py:455 | Blocked longer than `orchestrator_timeout_minutes` escalates to needs-human | config | D |

## Tier, executor, model
| Where | Rule today | Source | Class |
|---|---|---|---|
| labels.py:36 | No size label means m | literal | D |
| labels.py:40, runner.py:1252 | Tier climbs s→m→l per attempt; rebuild climbs too | literal | P |
| config.py:30-34 | Effort per tier: s medium, m high, l xhigh | config `tiers` | D |
| runner.py:2093-2097, 1256-1260 | Executor: lane `executor`, else global; Claude model: lane, tier, global | config | P — no per-issue override; edited by hand today |
| config.py:295 | Reviewer is the opposite executor | config | D (#186 makes it quota-aware) |

## Review strictness and merge
| Where | Rule today | Source | Class |
|---|---|---|---|
| runner.py:93-99, 139-144 | Review blocks only on Goal unmet, Out of scope touched, weakened checks; max 5 findings; diff cut at 60,000 chars | literal | weakened checks S, rest D (D77 A→B) |
| runner.py:1000 | `/code-review` level | config `review_level` | P |
| runner.py:1039, 1757-1761 | Unusable or timed-out review counts as FAIL | literal | P |
| policy.py:53, 55, 57 | Acceptance files, `always_human` paths, `risk:high` hold | config / label | S |
| policy.py:33-36, 59, runner.py:1600 | fast auto-merges; batch needs a parent; careful opens a PR | config `review` | P, but a human-review trade-off: Jin's axis |
| runner.py:1580-1632 | Merge only on CI pass or none; head change holds | literal | S |
| orch.py:251-286 | Human merge runs full tests, conflict check, CI first | config | S |

## Human calls, test selection, notifications
| Where | Rule today | Source | Class |
|---|---|---|---|
| runner.py:1304 | QUESTION.md from the executor blocks as spec-question | literal | D |
| scheduler.py:398 | Done when without a machine check blocks | literal | S |
| runner.py:466-561 | Acceptance baseline from the base, pytest only, cached 6 h | literal | presence S, 6 h D |
| runner.py:410-451 | Related tests: changed test files plus files that import a changed module directly | literal | D |
| inspect_actions.py:121-135 | Push once per new human item; morning summary at `notify_summary_hour` | config | D |

## Top five policy items for throughput (worked around today)
1. Lane order by issue number — faked with blocked_by links.
2. No lane pause/resume tool — `pause_lane` called directly, no record.
3. Per-repository ceiling and lane priority fixed — `.foragent.yml` edited by hand (machine AIMD stays a safety line).
4. Executor/model only per lane; tier climbs by attempt — config edited by hand; no per-issue override.
5. Global retry/restart budgets (`max_retries`, `review_max`, `conflict_retries`, one stall restart, two crashes, `max_runtime_minutes`) — the only override is `fa orch retry --hint`, which resets attempts.

## Coverage
Read fully: scheduler.py, runner.py, capacity.py, config.py, labels.py, actions.py, policy.py, orch.py; parts of state.py. Skimmed: tracker.py, executors.py, notify.py, inspect_actions.py, order.py, orch_reinstall.py, launchd.py. Not read: inspect_data.py `_push_item`, git.py, cli.py wiring, orch_github.py, board.py, watch/status/design/adopt/skills. cli.py may hold more judgments.
