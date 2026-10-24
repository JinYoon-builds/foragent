# Machine budgets read swap-ins, not kernel 'warn' alone

Evidence (2026-10-24 ~05:05-05:15 UTC, 16 GiB Mac): kern.memorystatus_vm_pressure_level stayed 2 (warn) on every sample while load was 0.6/CPU; over 20 s swap-ins were 0 and swap-outs 2,888. `capacity.machine_load()` returned ('warn', 0.63), so each tick removed a test slot and halved the worker budget: test-slots.json slots=1 and capacity.json parallel=1, held there while 6 runs and premerge queued on one slot. Kernel warn alone (compression) is not thrashing; swap-ins are. Only the last changed_at was stored, so falls and rises could not be counted.

- The sensor classifies: kernel critical -> critical; any swap-ins since the previous sample -> warn; kernel warn with zero swap-ins -> normal; kernel warn with no previous counter or an unreadable counter -> warn. A counter decrease (reboot) rebases the window. The previous counter lives in the machine data dir.
- New public seams `capacity.kernel_pressure()` and `capacity.swapins_total()`, read in-process (no subprocess, #292).
- The pure functions keep their meaning (locked: next_parallel halves on warn/critical; test slots step down one per tick).
- Every change of either machine budget appends one line to `~/.local/share/foragent/budget-history.jsonl` (at, budget, from, to, pressure, load_per_cpu, swapins) for `fa orch facts` (#333).

Built outside the gate by the planning session (gate speed fixes).
