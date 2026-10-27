# fa-architect memory

Durable lessons and proposal records of this project's architect. Only the architect session
writes this file.

## Proposals

- 2026-10-27 r18: #370 stop = thin caller of the stall-kill path (`_kill_run(outcome='stopped')` under the tick lock + `_restore_stopped`), new branch from main, D95 dropped, stall-kill races named as non-goals (Decision: person, A/B/C/D) - expected: #370 merges in ≤1 run (was 0/11, 6 holds), diff ≤~150 lines (was +2099), 0 wip-preservation-failed events stay 0; measured: pending

## Lessons

- A feature that repeats the reviewer hold "found defects after 3 rounds" with a new race each time usually has a Goal that sets a higher bar than the path it reuses. Name the inherited races as non-goals. Do not add more ordering rules. (#370, 9 holds)
- Production state: `~/.local/share/foragent/JinYoon-builds__foragent/` (runs.jsonl, orch-events.jsonl). `fa orch facts --json` does not list each issue's runs, so read runs.jsonl for those.
