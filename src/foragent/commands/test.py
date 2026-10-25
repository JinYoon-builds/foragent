"""`fa test`: wait for a machine-wide test slot, then run pytest."""

import argparse
from pathlib import Path
import subprocess
import sys

from foragent.commands import facade

ORDER = 160

TEST_USAGE = "fa test [--repo-root DIR] <pytest args>"


def add(commands) -> None:
    # Listed for `fa --help` only; main() hands `fa test ...` to pytest before parsing.
    test = commands.add_parser(
        "test", help="Wait for a machine-wide test slot, then run pytest with the given arguments",
        usage=facade().TEST_USAGE, add_help=False,
    )
    test.add_argument("pytest_args", nargs=argparse.REMAINDER)


def _test_command(argv: list[str]) -> int:
    """`fa test`: everything after `test` goes to pytest, except a leading --repo-root."""
    cli = facade()
    root = None
    if argv[:1] == ["--repo-root"] and len(argv) > 1:
        root, argv = Path(argv[1]), argv[2:]
    elif argv and argv[0].startswith("--repo-root="):
        root, argv = Path(argv[0].split("=", 1)[1]), argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if root is None:
        cwd = Path.cwd()
        root = next((path for path in (cwd, *cwd.parents) if (path / ".foragent.yml").is_file()), cwd)
    root = root.resolve()
    try:
        return cli.run_tests(root, cli.load_config(root), argv)
    except (cli.ConfigError, OSError, subprocess.SubprocessError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


def run(args) -> int:
    return facade()._test_command(args.pytest_args)
