---
name: project-123-baseline-survivors
description: #123 dropped-id audit after a base sync — one exact collect-only listing in follow_base; why per-id probing was deleted
metadata:
  type: project
---

Final design (orchestrator redesign 2026-10-27): `run_implementation.follow_base()` (#483) first calls
`run_baseline.collect_protected_ids()`. It runs ONE `<baseline command> <acceptance_dir> --ignore=<new base exclusions>
--collect-only ` on a detached checkout of the new base. The plugin is loaded by a helper TEST MODULE
(`.foragent-collect/test_fa_collect.py`, passed first on the command line) that puts its own directory on sys.path and sets
`pytest_plugins = ["fa_collected_ids"]`: works with `python -I`, `--noconftest`, `-p no:terminal`, `env PYTHONPATH=…`, any import
mode and any python_files (explicit files always collect). Rejected: PYTHONPATH injection (`env` overrides, `-I` ignores) and a
helper conftest (`--noconftest`). The listing path is baked into the plugin source. After collecting,
`_validate_baseline_checkout()` runs like after a capture (a conftest that deletes tracked tests → #327). Collection always runs, even when every id is owned. Missing = protected id not in that exact set (rootdir-relative ids are
converted to repo-relative). Exit 5 or no acceptance dir → empty set. A collection error, another exit, no listing or a
timeout → #327 stop, but only after #483's capture has run once (locked `test_unusable_new_base_baseline_stops_with_327_outcome`
asserts one capture at the moved sha; that unusable capture usually stops the run itself). importorskip skips are not errors. No id left → no re-capture, empty baseline (emptiness derived from
`attempt.baseline.node_ids`, never stored; an empty set stays empty), verification-skip line logged by `follow_base`.
Otherwise #483's re-capture runs and `log_dropped_ids()` writes one English line per id: owned > missing > failing (only when failing_ids is known and lists the id) > no longer protected
(CI mode, collected_ids or failing_ids None).

**Why:** many review rounds of per-id probing (selector exit codes, bisection, file/directory controls, JUnit, deselects) each
found a case where selector success or failure was mistaken for exact membership (`::test_keep` still selects `test_keep[1]`;
a transient ImportError followed by a clean file probe). The orchestrator redesigned to one exact listing. Never infer membership
from probe results again.
**How to apply:** no `--junitxml` in this step — locked `test_baseline_follows_base.py` counts every JUnit command as a capture,
and prebuild's harness forbids JUnit in the issue worktree. Prebuild's `test_main_move_prebuilds_new_sha_while_old_run_keeps_old_baseline`
already failed on main (6ea1f52); compare against main before calling it a regression. A main test asserts no
"Acceptance baseline verification" line for a baseline that starts empty, so `run_checks` stays as on main.
