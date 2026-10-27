# Memory threshold (Jin, 2026-10-25 07:05)

Evidence: 1,621 overnight samples; swap-ins above 100 pages in only 20 windows and never sustained; kernel level never left normal; free memory never below 56%; per-run RSS median 57 MB, p90 658 MB, max 1.3 GB. Today any swap-in in a window lowers the budgets, so temporary floors (test_slots_floor, parallel_floor) were pinned. New policy: back off only on sustained memory signals; admit a new run only with memory headroom; drop the floors; record which rule fired. CPU-load behaviour is unchanged.

Units, each verifiable alone: memory-backoff (sustained triggers, floors ignored, history rule names) and memory-headroom (admission by free memory minus a per-run reservation). Older locked budget tests are amended with their names kept. After memory-backoff merges, the floor lines come out of this repository's .foragent.yml.
