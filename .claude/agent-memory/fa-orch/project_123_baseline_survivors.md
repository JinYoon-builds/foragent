---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `_base_moved()` says HEAD (or MERGE_HEAD) merged `origin/<target>` commits newer than `baseline.base_sha` (merge-base not an ancestor of base_sha; git errors count as moved). Review rejected a path gate on the acceptance dir: a root conftest.py can rename parametrized ids. fast_checks fakes need Done when → baseline order when the base has not moved. Tests must build history with plumbing (commit-tree/update-ref); conftest forbids `git commit`. `_missing_ids()` uses no instrumentation: the verification's own
`<baseline.command> --collect-only <ids>` and only the exit status is read (pytest exits 4 for a selector not found). Groups that
exit 4 are halved to single ids. Every project setting (conftest imports, --noconftest, -p, -I, PYTHONPATH, python_files, terminal
options) applies as in verification. Rejected designs, each broken by a review: terminal parsing, directory collection, PYTHONPATH
plugin injection, `shlex.join` of `NAME=value`, plugin as a helper-dir conftest.py (overwrote the project's `conftest` module;
`--noconftest` disabled it). `--collect-only` JUnit has no testcases; `--setup-plan` crashes without the terminal plugin (checked).
Verification after a sync (`exact_check=True`) runs exact ids only: each non-parametrized id gets `--deselect=<id>[` and
`--deselect=<id>::` (`_expansion_deselects`; skipped if another baseline id shares the prefix; a param id `f[a]` selects exactly,
so it gets none — a deselect built from `a` would drop `a][b`). Expanded variants therefore never execute (a variant calling
`pytest.exit` cannot block). pytest's nonzero exit always stands — never rewrite 1/2 to success from test records (session hooks can
set exitstatus). ONE deletion rule, `_proven_absent()`: `<cmd> --collect-only <id> <its deselects>` with a `--collect-only` JUnit report must exit
4 (not found) or 5 (only expansions) and the report must hold zero records (collect-only JUnit records collection skips/errors —
importorskip, `pytest_generate_tests` skip, import errors — never items). Hooks can fake both codes: a hook raising
`pytest.UsageError` exits 4, a hook deselecting the test exits 5. So exit 4 also needs the id's nearest existing directory to
collect without a usage error (exit 0/2/5, a cached control), and exit 5 needs the id to have expansions and the bare selector
(without our deselects) to exit 0. No disk checks as evidence: session hooks can create test files
(sessionstart) and remove them (sessionfinish). Timeouts, missing report, any other exit → unverifiable. Groups exiting 4 are
bisected first; ids absent from the verification JUnit are probed whatever the exit (detection logs even when another test fails).
exit 5 passes only when every id is proven deleted. Unverifiable ids leave the
selection (others run) and fail the check with a note. Parse ids with `_node_parts()` (params may contain `::`).
Without a sync the command is unchanged (fast_checks fakes treat any `--junitxml=` as D53 discovery).
`run_checks` filters a local copy each verification and never overwrites `attempt.baseline`: a later sync can restore a dropped id
(`tests/test_baseline_restore_unit.py`, fails if the copy is written back). No existing file drops all ids without pytest.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines; unproven absence fails as unverifiable.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
After 10+ review rounds each found a new hook that forges a pytest signal (UsageError, deselect, sessionstart files, UsageError only
for node selectors). Hooks can forge any signal, so on 2026-10-26 this went to `.foragent/QUESTION.md` (trust hooks like the
verification run does, recommended). Don't keep adding controls for adversarial hooks without that decision.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
