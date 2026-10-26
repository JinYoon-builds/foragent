---
name: project-123-baseline-survivors
description: #123 baseline node ids that vanish after a base sync are dropped via collect-only before baseline verification
metadata:
  type: project
---

`run_baseline.surviving_baseline()` runs in `run_checks._verify_implementation()` before every baseline verification.
It runs only when `_base_moved()` says HEAD (or MERGE_HEAD) merged `origin/<target>` commits newer than `baseline.base_sha` (merge-base not an ancestor of base_sha; git errors count as moved). Review rejected a path gate on the acceptance dir: a root conftest.py can rename parametrized ids. fast_checks fakes need Done when → baseline order when the base has not moved. Tests must build history with plumbing (commit-tree/update-ref); conftest forbids `git commit`. It collects the baseline ids' existing files, named explicitly (`<cmd> <files> --collect-only`; pytest collects explicit files
even when `python_files` changed; the command pins `--rootdir=.`, so ids are worktree paths), with the existing `ci_pytest.py`
measurement plugin (copied as `fa_baseline_collection`, `FA_REPORT`/`FA_SHARD=0`/`FA_SHARDS=1`), reading `collected_ids` from its JSON.
`_with_plugin_path()` prepends the plugin dir to every inline `PYTHONPATH=` before the pytest word (an inline one overrides an outer one).
Reviews rejected terminal parsing (`verbosity_test_cases=0` tree, `-p no:terminal` rejects `--verbosity`) and directory collection.
No existing file drops all ids without pytest. Scratch dir is `<state>/runs`.
Missing ids get `Acceptance baseline dropped missing node id: <id>` log lines. Collection failure (exit not 0/5, missing JSON, exit mismatch) keeps all ids,
so import errors are not hidden.
The acceptance test allows `--collect-only` commands but no pytest execution when all ids vanish.
**Why:** a stale id makes pytest exit 4 "not found" and hides real failures.
**How to apply:** keep baseline filtering per verification; renamed tests are not re-protected (out of scope).
