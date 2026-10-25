---
name: cli-commands-package
description: #429 fa command groups live in foragent/commands/<name>.py (ORDER/add/run), cli.py is a thin facade; patch rules and help-diff check
metadata:
  type: project
---

#429 `cli._parser()` builds its subparsers from `foragent.commands.discover()`. That function runs `pkgutil.iter_modules(__path__)` on every call and sorts the modules by `(ORDER, __name__)`. Each group module has `ORDER` (10–180 in the original help order), `add(commands)` and `run(args)`. The module file name is the command name, and `main` dispatches through `commands.module(args.command)`. Shared helpers (`facade`, `repo_store`, `_open_lane_view`) live in `commands/__init__.py`. A helper module inside the package would itself be discovered as a command, so don't add one.

**Why:** The locked `test_cli_split` sets cli.py to 150 lines or fewer, and each module under `commands/` to 300 or fewer. It also requires that every CLI_NAMES entry stays a `cli` attribute, and that patching `cli.check_done`, `set_profile`, `run_tests`, `init_repo`, `_comma_keys` and similar names reaches the handlers. It also copies `commands/bench.py` with the string "bench" replaced and appends it to `__path__` (so the bench module must call through `cli.bench.run`).

**How to apply:** Handlers read every dependency at run time through `cli = facade()` as `cli.<name>`. Helpers moved out of cli (`_status`, `_print_status`, `_tidy`, `_orch_inbox`, `_inspect`, `_test_command`, `TEST_USAGE`, `_comma_*`, `_offer_statusline`) are re-exported from cli, and callers also go through `cli._x`. The statusline fast path and `--uninstall` stay outside the main error-catching `try`, as before. Behaviour check: write a script under `.foragent/` that runs `--help` for every subcommand. Run it once against `git archive HEAD src`, extracted into `.foragent/base`, and once against the current tree, then `diff` the output (981 lines identical).
