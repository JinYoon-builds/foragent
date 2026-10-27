---
name: project-449-overlap-facts
description: #449 overlap facts in fa orch facts — where file_conflicts is computed, 30-line human limit, zsh test-list pitfall
metadata:
  type: project
---

- `orch_facts_runs._row()` adds `declared_file_count`/`changed_file_count` (None when the Run field is None) and `overlap_records`. `_file_conflicts(rows)` counts only finished+merged rows, already deduped per execution and limited to the 7-day window, so old runs drop out.
- Human facts output is capped at 30 lines (`test_orch_facts.py:237,579`). New facts go as one line appended to an existing block, not a new block.
- The facts acceptance suite (baseline_trend, repeated_work, resource_facts) had 32 failures before #449 too; compare FAILED sets before/after by `git diff > p; git apply -R p; ...; git apply p`.
- zsh pitfall: `T="a b"; fa test $T` passes one argument. Use an array `T=(a b)`.
