---
name: project-123-baseline-survivors
description: #123 dropped-id audit after a base sync — one exact collect-only listing in follow_base; why per-id probing was deleted
metadata:
  type: project
---

Final design (orchestrator redesign 2026-10-27): `run_implementation.follow_base()` (#483) first calls
`run_baseline.collect_protected_ids()`. It runs ONE `<baseline command> <acceptance_dir> --ignore=<new base exclusions>
--collect-only ` on a detached checkout of the new base. The plugin (orchestrator decision 2026-10-27) is a module with a
private random name `_foragent_nodeids_<16 hex>` in a temp dir OUTSIDE the checkout, given only to the collection subprocess via
PYTHONPATH (`_with_python_path()` joins leading and post-`env` assignments, `NAME=<quoted>`) and loaded with `-p <name>`. Rejected
because they polluted project imports or broke launchers: a helper test module `test_fa_collect` on sys.path (import file
mismatch with a project file of that name), a helper conftest (`--noconftest`), a fixed module name. Known limit: `python -I`
ignores PYTHONPATH → the plugin cannot load → #327 stop, nothing dropped. The listing path is baked into the plugin source. After collecting,
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
