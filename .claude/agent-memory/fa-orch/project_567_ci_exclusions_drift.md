---
name: project-567-ci-exclusions-drift
description: #567 ci_mode shard evidence accepts CI exclusions that are a subset of the gate's current exclusions; consistency and timeout pitfalls
metadata:
  type: project
---

- `ci_mode.scope_covers(recorded, required)`: same keys, every non-`excluded` field equal, recorded `excluded` ⊆ required. Used by `comparison()` (payload scope) and `ComparisonEvidence.current()` (stored gate scope).
- A bare subset rule is not enough: `test_ci_mode.py[missing-exclusions]` empties one shard's `payload["excluded"]` and expects rejection. Keep `payload["scope"]["excluded"] == payload["excluded"]` (was implied by the old exact equality).
- Tests in files CI excluded are still rejected as "excluded tests"; tests in files excluded only now are dropped from `lost`.

**Why:** order publishes during CI grew the gate's exclusion list and discarded all in-flight evidence (#127 on 2026-10-26).

**How to apply:** under machine load (~19), `tests/acceptance/test_ci_mode.py` took 26 min for 22 cases. The Stop hook's Done-when check can report "Check timed out" — that is not a failure. Run in background with a long timeout and write to a log under /private/tmp.
