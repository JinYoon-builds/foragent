---
name: cli-commands-package
description: #429 fa command groups live in foragent/commands/<name>.py (ORDER/add/run), cli.py is a thin facade; patch rules and help-diff check
metadata:
  type: project
---

#429 `cli._parser()` builds its subparsers from `foragent.commands.discover()`. That function runs `pkgutil.iter_modules(__path__)` on every call and sorts the modules by `(ORDER, __name__)`. Each group module has `ORDER` (10–180 in the original help order), `add(commands)` and `run(args)`. The module file name is the command name, and `main` dispatches through `commands.module(args.command)`. Shared helpers (`facade`, `repo_store`, `_open_lane_view`) live in `commands/__init__.py`. A helper module inside the package would itself be discovered as a command, so don't add one.

**Why:** The locked `test_cli_split` sets cli.py to 150 lines or fewer, and each module under `commands/` to 300 or fewer. It also requires that every CLI_NAMES entry stays a `cli` attribute, and that patching `cli.check_done`, `set_profile`, `run_tests`, `init_repo`, `_comma_keys` and similar names reaches the handlers. It also copies `commands/bench.py` with the string "bench" replaced and appends it to `__path__` (so the bench module must call through `cli.bench.run`).

**How to apply:** Handlers read every dependency at run time through `cli = facade()` as `cli.<name>`. Helpers moved out of cli (`_status`, `_print_status`, `_tidy`, `_orch_inbox`, `_inspect`, `_test_command`, `TEST_USAGE`, `_comma_*`, `_offer_statusline`) are re-exported from cli, and callers also go through `cli._x`. The statusline fast path and `--uninstall` stay outside the main error-catching `try`, as before. Behaviour check: write a script under `.foragent/` that runs `--help` for every subcommand. Run it once against `git archive HEAD src`, extracted into `.foragent/base`, and once against the current tree, then `diff` the output (981 lines identical).

#429 verification: under high load (load average 12–14), the Done when bundle (61 files, 367 tests) takes about 17 minutes. Each stop-hook check-done run stopped with "Check timed out" at 26–79% of the way through. One run showed an F in `test_premerge_slots`, but that file passes when run alone (17 passed) and also passed in the full run. While waiting, don't start your own `fa test` in parallel, because it competes with the hook check for the machine-wide test slot and the CPU. To find out whether the code is at fault, run only the affected file on its own.

#429 review fix: the review flagged command modules that imported `json`, `replace`, `asdict`, `os`, `sys`, `Path`, `argparse`, `SimpleNamespace`, `subprocess` or `orch_inbox` directly, and a module that called a re-exported helper (`_uninstall_statusline`) directly instead of through the facade. These bypass patches on cli. Every name that was a cli global is now read as `cli.<name>`, and the only imports in the modules are `facade` and `repo_store`. `tests/test_cli_commands.py` checks this with an AST guard plus patch-reachability tests. Annotations are evaluated when the module loads, so `cli.` must not appear in them; that is why the annotations were removed.
