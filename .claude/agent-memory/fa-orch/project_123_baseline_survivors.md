---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `_base_moved()` says HEAD (or MERGE_HEAD) merged `origin/<target>` commits newer than `baseline.base_sha` (merge-base not an ancestor of base_sha; git errors count as moved). Review rejected a path gate on the acceptance dir: a root conftest.py can rename parametrized ids. fast_checks fakes need Done when → baseline order when the base has not moved. Tests must build history with plumbing (commit-tree/update-ref); conftest forbids `git commit`. `_missing_ids()` uses no instrumentation: ids whose file is gone are missing without pytest; the rest run the verification's own
`<baseline.command> --collect-only <ids>` and only the exit status is read (pytest exits 4 for a selector not found). Groups that
exit 4 are halved to single ids. Every project setting (conftest imports, --noconftest, -p, -I, PYTHONPATH, python_files, terminal
options) applies as in verification. Rejected designs, each broken by a review: terminal parsing, directory collection, PYTHONPATH
plugin injection, `shlex.join` of `NAME=value`, plugin as a helper-dir conftest.py (overwrote the project's `conftest` module;
`--noconftest` disabled it). `--collect-only` JUnit has no testcases; `--setup-plan` crashes without the terminal plugin (checked).
Verification after a sync (`exact_check=True`) runs exact ids only: each non-parametrized id gets `--deselect=<id>[` and
`--deselect=<id>::` (`_expansion_deselects`; skipped if another baseline id shares the prefix; a param id `f[a]` selects exactly,
so it gets none — a deselect built from `a` would drop `a][b`). Expanded variants therefore never execute (a variant calling
`pytest.exit` cannot block). pytest's nonzero exit always stands — never rewrite 1/2 to success from test records (session hooks can
set exitstatus). An id absent from a successful/empty run is dropped only if `<cmd> --collect-only <id> <its deselects>` exits 5
with a `--collect-only` JUnit report holding zero records; exit 5 passes only when every id is proven deleted. `_missing_ids()`
deletes an exit-4 id only when its file is gone or the whole file collects (exit 0 or 5) with zero JUnit records: collect-only JUnit
records collection skips/errors (importorskip, `pytest_generate_tests` skip) even when the exit is 0. Unverifiable ids leave the
selection (others run) and fail the check with a note. Parse ids with `_node_parts()` (params may contain `::`).
Without a sync the command is unchanged (fast_checks fakes treat any `--junitxml=` as D53 discovery).
`run_checks` filters a local copy each verification and never overwrites `attempt.baseline`: a later sync can restore a dropped id
(`tests/test_baseline_restore_unit.py`, fails if the copy is written back). No existing file drops all ids without pytest.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines; unproven absence fails as unverifiable.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
