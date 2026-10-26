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
The verification command stays `<command> <ids>`: locked `test_baseline_prebuild.py` treats any `--junitxml=` command as a
baseline build (forbidden in the issue worktree) and checks that its `::` words equal the protected ids, so no JUnit and no
`--deselect` there. Expansion (`f` → `f[...]` or a class) is settled before the run by `_replaced_ids()`: bare selector exit 0
and, with `--deselect=<id>[`/`--deselect=<id>::`, exit 5 → dropped; only for ids whose test file, a conftest.py on its path or
pytest config changed (`_changed_paths`; unknown → all). An exact `f` cannot coexist with `f[...]`, so kept ids select exactly.
pytest's exit always stands; `protected_result()` fails unverifiable ids. ONE deletion rule, `_proven_absent()`: `<cmd> --collect-only <id> <its deselects>` with a `--collect-only` JUnit report must exit
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
Trust decided 2026-10-26 (spec update): pytest's own 'not found' (exit 4 + no record + directory control) is trusted like the
verification run; deliberate forgery hooks → REBUTTAL. Then main's #483 (`run_publish.refresh_base` recaptures on a non-neutral base move) clashed with #123's locked tests 2/3.
Resolved without editing locked tests by `run_baseline.follow_moved_base()`: first check the old ids on a detached checkout of the
new base with collection only (same deletion rule, logs drops); none left → empty baseline, no capture (spec: "skips when none
remain"; test 3 forbids any non-collect pytest after the sync); else capture (#483) and keep protecting old ids the capture lacks
while their test file blob is unchanged (test 2), dropping changed ones (#483 `fails-on-new-base`).
Review after that (2026-10-26): recapture protects renamed tests and drops still-existing failing tests (both against #123's
goal/out of scope) but #483's locked `fails-on-new-base` requires the drop; plus the path gate for expansion probes misses
helpers/plugins. Sent to QUESTION.md (recommend #123 governs, re-approve #483 test; probe all non-param ids). Fixed meanwhile:
directory control needs exit 0/5 and no error record; `_changed_paths` uses `git diff -z`.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
