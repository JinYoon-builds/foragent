# fa-architect memory

Durable lessons and proposal records of this project's architect. Only the architect session
writes this file.

## Proposals

- 2026-10-27 r18: #370 stop = thin caller of the stall-kill path (`_kill_run(outcome='stopped')` under the tick lock + `_restore_stopped`), new branch from main, D95 dropped, stall-kill races named as non-goals (Decision: person, A/B/C/D) - expected: #370 merges in ≤1 run (was 0/11, 6 holds), diff ≤~150 lines (was +2099), 0 wip-preservation-failed events stay 0; measured 2026-10-29: merged via PR #822 on 10-27 after 2 runs (timeout, merged), +475/-11, 0 real wip-preservation-failed events (the 2 grep hits are a hint and r18's own text). Partial: runs and diff above target, safety held
- 2026-10-29 r19: #186 headroom only stable-sorts [claude, codex] before #786 decide(), and only when no order is written (no `executors`/`claude_quota`) and both have usage; used-up window = hard limit; kept executor released at limit; Run stores the concrete pair (tick, once per start) in place of implementer; no locked-test edits (Decision: orchestrator) - expected: preread unknowns 20->0 needing a person, merge <=2 runs, #786 locked tests green, headroom executor-switch events appear; measured: pending
- 2026-10-29 r20: #238 tier = f(run.executor, shared headroom helper); no usage -> size tier; last attempt l; runner stays tier writer; after #186 (Decision: orchestrator) - expected: preread unknowns 12->0 needing a person, merge <=2 runs, attempt_tiers show l for ample s/m; measured: pending

## Lessons

- A feature that repeats the reviewer hold "found defects after 3 rounds" with a new race each time usually has a Goal that sets a higher bar than the path it reuses. Name the inherited races as non-goals. Do not add more ordering rules. (#370, 9 holds)
- Production state: `~/.local/share/foragent/JinYoon-builds__foragent/` (runs.jsonl, orch-events.jsonl). `fa orch facts --json` does not list each issue's runs, so read runs.jsonl for those.
- Before stacking a feature on a merged one, run the new locked test's expectations against the merged feature's locked tests. Conflicts show up as concrete cases (#186 vs #786: claude 79% under keep 80 must win), and the reconciling rule (here: a written order beats headroom) follows from them.
- Locked acceptance tests (`tests/acceptance/*`, "do not edit") constrain every later proposal. Check `grep -ln` there before you propose a behaviour change; unit tests may change.
