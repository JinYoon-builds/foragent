# Throughput before the speed work (baseline, 2026-10-24)

"Before" numbers for the speed work (D81 #307, #221, #295, D80). Source:
`runs.jsonl` of JinYoon-builds/foragent (latest entry per run, finished runs
only, bucketed by the KST date of the run's last progress). Outside merges by
the planning session are not in this file. Script: `uv run --no-project python docs/research/throughput.py ~/.local/share/foragent/JinYoon-builds__foragent/runs.jsonl`.

## Per day

| Day (KST) | Finished runs | Merged | Merged share | Median merged run (min) | p90 (min) | Test-slot/limit waits (min) |
|---|---|---|---|---|---|---|
| 10-21 | 45 | 28 | 62 % | 10 | 24 | 28 |
| 10-22 | 24 | 14 | 58 % | 27 | 37 | 75 |
| 10-23 | 76 | 17 | 22 % | 33 | 84 | 32 |
| 10-24 (to 10:00) | 24 | 10 | 42 % | 73 | 98 | 313 |

Outcomes:
- 10-21: merged 28, held 12, retries-exceeded 3, ci-failed 1, crashed 1
- 10-22: merged 14, retries-exceeded 4, spec-question 3, error 2, held 1
- 10-23: merged 17, timeout 15, retries-exceeded 10, crashed 10, error 10, spec-question 7, killed 4, other 3
- 10-24: merged 10, timeout 7, crashed 3, spec-question 2, retries-exceeded 1, stalled-restart 1

Merged by size tier: 10-21 s10 m11 l7; 10-22 s5 m4 l5; 10-23 s6 m9 l2; 10-24 s5 m5.

## Merged per 3 hours (KST)

10-21 12h 14, 15h 13, 21h 1 · 10-22 15h 2, 18h 4, 21h 8 · 10-23 00h 6, 03h 3,
06h 3, 09h 1, 12h 2, 21h 2 · 10-24 00h 3, 03h 2, 06h 4, 09h 1.

## Reading

- The median time of a merged run grew about 7x in four days (10 → 73 min)
  while the tier mix stayed similar. The checks grew, not the issues: the unit
  suite and the acceptance directory grew with every order, related-test
  selection picked ~40 files for central modules, and runs competed for the
  machine.
- 10-23 lost most runs to plumbing, not to code quality: 15 timeouts (checks
  hitting the run budget), 10 crashes (stale crashed rows, #277/#295), 10 errors
  (the unverified Claude worker switch before #247). Only 22 % of runs merged.
- 10-24 shows the remaining cost: 7 of 24 runs timed out, and waits for test
  slots and limits reached 313 minutes. This is what D81 (#307) targets.

## Fixes measured against this baseline

#106 machine budget, #243 test slots, #257 baseline cache, #270 WIP
preservation, #145 lane clones (landed before or during this window);
#307 CI-trusted checks, #221 changed-only reads, #295 snapshot re-read, D80
plan and queue tools (after). Re-run the same script after they land and add
an "after" table here.
