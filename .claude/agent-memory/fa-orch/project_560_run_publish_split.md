---
name: project-560-run-publish-split
description: run_publish.py facade split (#560) — module layout, call-time run_publish.X lookup, how to port main edits
metadata:
  type: project
---

run_publish.py is a facade: it keeps all the original imports and re-exports from `run_publish_ci` (CI wait/failure), `run_publish_merge` (merge lock, final inputs, batch PR, retitle, check_publication), `run_publish_pr` (PR body/push/open-update), `run_publish_preserve` (WIP preservation), `run_publish_flow` (`_publish`/`_publish_locked`) and `run_publish_reviewed`. Each split module puts `from foragent import run_publish` at the **bottom** of the file, so a module imported directly still loads. Function bodies call every original module global as `run_publish.X`. Decorators and dataclass bodies import their names directly, because they run at import time.

**How to port a main edit:** run `git diff <merge-base> MERGE_HEAD -- src/foragent/run_publish.py` and apply each hunk to its split module with the prefix. Add any new import to the facade. Then check with AST: strip `run_publish.`, compare each def with main's copy and confirm `hasattr` for every name in main.

**Squashed-merge conflicts (2026-10-27):** the runner's commits keep earlier main merges as plain commits, so the merge base stays old. Main's own later edits then conflict with themselves in files this issue never touched. Check with `git diff <previously merged main> HEAD -- <file>`: empty means take `git show MERGE_HEAD:<file>`. Files Git merges cleanly can also come out broken: `tests/conftest.py` had a fixture defined twice. Before finishing, use `git diff MERGE_HEAD --name-only` to confirm that only this issue's files differ from main.
