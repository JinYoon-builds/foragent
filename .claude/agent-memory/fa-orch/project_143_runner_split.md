---
name: 143-runner-split
description: #143 runner.py split into runner_core/prompts/checks/review/attempt/publish; facade module class propagates monkeypatches to sibling modules
metadata:
  type: project
---

#143 split `runner.py` (2,222 lines) into `runner_core` (context, failures, executor waits, `_check`), `runner_prompts`, `runner_checks` (pytest/baseline/check_done), `runner_review` (lane skills, `_review`, `_review_only`), `runner_attempt` + `runner_publish` (`_implement_and_publish` as `_Attempt`/`_Publication` classes). `runner.py` keeps the original imports, re-exports every name, and holds `_execute`, `review_only`, `run_issue`.

**Patch rule:** `runner.py` sets `sys.modules[__name__].__class__ = _RunnerFacade`. Its `__setattr__`/`__delattr__` forward to every sibling module whose globals or builtin reads contain the name. The target sets are computed once at import (`_PATCH_TARGETS`), so delete-then-restore and `mock.patch("foragent.runner.print")` still reach the siblings. Tests keep patching `foragent.runner.X` only.

**Why:** tests patch ~12 runner names (`_check`, `run_logged`, `_remaining`, `kick_tick`, `after_merge`, `poll_executor`, `detect_test_commands`, ...). Moved code binds its own module globals, so without forwarding those patches stop reaching `run_issue`.

**How to apply:** when adding a runner function, put it in the sibling module that matches its role. Keep `runner.py` ≤500 and siblings ≤600 lines (`tests/acceptance/test_runner_split.py`). New sibling modules must be added to `_SPLIT_MODULES`. In `_Attempt`, state that the old closures shared through `nonlocal` is kept on `self`. Locals that shadowed outer names stay local (for example `paths` in `prepare_target`, `issue` in `publish_locked`).
