---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `git diff --cached <baseline.base_sha> -- <acceptance_dir>` is non-empty (fast_checks fakes need Done when → baseline order). It collects the same selection as the capture (`<cmd> <acceptance_dir> --ignore=<excluded>`) with `--collect-only --verbosity=-1`
(absolute verbosity overrides `-q` from addopts) and reads `::` lines from the log after the offset.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines. Collection failure (exit not 0/5) keeps all ids,
so import errors are not hidden. Missing acceptance dir drops all without running pytest.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
