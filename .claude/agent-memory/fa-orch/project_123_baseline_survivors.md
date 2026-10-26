---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `_base_moved()` says HEAD (or MERGE_HEAD) merged `origin/<target>` commits newer than `baseline.base_sha` (merge-base not an ancestor of base_sha; git errors count as moved). Review rejected a path gate on the acceptance dir: a root conftest.py can rename parametrized ids. fast_checks fakes need Done when → baseline order when the base has not moved. Tests must build history with plumbing (commit-tree/update-ref); conftest forbids `git commit`. `_missing_ids()` uses no instrumentation: ids whose file is gone are missing without pytest; the rest run the verification's own
`<baseline.command> --collect-only <ids>` and only the exit status is read (pytest exits 4 for a selector not found). Groups that
exit 4 are halved to single ids; an id is missing only if `<cmd> --collect-only <its file>` exits 0/5 (an import-failing file exits
4 per id but 2 per file → kept). Every project setting (conftest imports, --noconftest, -p, -I, PYTHONPATH, python_files, terminal
options) applies as in verification. Rejected designs, each broken by a review: terminal parsing, directory collection, PYTHONPATH
plugin injection, `shlex.join` of `NAME=value`, plugin as a helper-dir conftest.py (overwrote the project's `conftest` module;
`--noconftest` disabled it). `--collect-only` JUnit has no testcases; `--setup-plan` crashes without the terminal plugin (checked).
A valid selector is not an exact id: `::test_keep` still selects `test_keep[1]`. So after a sync (`exact_check=True`)
`baseline_verification_command()` adds `--deselect=<id>[`/`--deselect=<id>::` (pytest deselects by prefix) plus a legacy JUnit
report in `<state>/runs`; `exact_baseline_result()` logs ids JUnit did not record and turns an empty run (exit 5) into a skip.
Only for exit 0/1/5. Without a sync the command is unchanged (fast_checks fakes treat any `--junitxml=` as D53 discovery).
`run_checks` filters a local copy each verification and never overwrites `attempt.baseline`: a later sync can restore a dropped id
(`tests/test_baseline_restore_unit.py`, fails if the copy is written back). No existing file drops all ids without pytest.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines; unproven absence keeps the id.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
