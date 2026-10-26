---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `_base_moved()` says HEAD (or MERGE_HEAD) merged `origin/<target>` commits newer than `baseline.base_sha` (merge-base not an ancestor of base_sha; git errors count as moved). Review rejected a path gate on the acceptance dir: a root conftest.py can rename parametrized ids. fast_checks fakes need Done when → baseline order when the base has not moved. Tests must build history with plumbing (commit-tree/update-ref); conftest forbids `git commit`. It collects the baseline ids' existing files, named explicitly (`<cmd> <files> <helper dir> --collect-only`; pytest collects explicit
files even when `python_files` changed; the command pins `--rootdir=.`, so ids are worktree paths).
The `ci_pytest.py` measurement plugin is copied as `conftest.py` into a temp dir under the worktree's runner-owned `.foragent/`, and
that dir is passed as a pytest argument: pytest imports it itself, so `python -I` (ignores PYTHONPATH) and inline `PYTHONPATH=` can't
hide it. `FA_REPORT`/`FA_SHARD=0`/`FA_SHARDS=1` env; read `collected_ids` from its JSON; the dir is removed in `finally`.
Earlier rejected designs: terminal parsing (`verbosity_test_cases=0`, `-p no:terminal`), directory collection, PYTHONPATH injection,
`shlex.join` of `NAME=value` words (quotes the assignment whole → exit 127). `--collect-only` JUnit has no testcases (checked).
`run_checks` filters a local copy each verification and never overwrites `attempt.baseline`: a later sync can restore a dropped id
(`tests/test_baseline_restore_unit.py`, fails if the copy is written back). No existing file drops all ids without pytest.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines. Collection failure (exit not 0/5, missing JSON, exit mismatch) keeps all ids,
so import errors are not hidden.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
