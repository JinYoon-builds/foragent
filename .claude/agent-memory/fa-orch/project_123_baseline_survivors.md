---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `_base_moved()` says HEAD (or MERGE_HEAD) merged `origin/<target>` commits newer than `baseline.base_sha` (merge-base not an ancestor of base_sha; git errors count as moved). Review rejected a path gate on the acceptance dir: a root conftest.py can rename parametrized ids. fast_checks fakes need Done when → baseline order when the base has not moved. Tests must build history with plumbing (commit-tree/update-ref); conftest forbids `git commit`. It collects the same selection as the capture (`<cmd> <acceptance_dir> --ignore=<excluded>`) with `--collect-only --verbosity=-1`
(absolute verbosity overrides `-q` from addopts) and reads `::` lines from the log after the offset.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines. Collection failure (exit not 0/5) keeps all ids,
so import errors are not hidden. Missing acceptance dir drops all without running pytest.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
