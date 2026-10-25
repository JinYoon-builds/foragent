"""`fa doctor --live`: check that the executors can do real work."""

from foragent.commands import facade

ORDER = 40


def add(commands) -> None:
    cli = facade()
    doctor = commands.add_parser("doctor", help="Check that the executors can do real work")
    doctor.add_argument("--live", action="store_true", required=True,
                        help="Run each executor once on a throwaway task in a temporary clone")
    doctor.add_argument("--executor", choices=("codex", "claude"),
                        help="Executor to check (default: the configured executor)")
    doctor.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    config = cli.load_config(root)
    checks = [cli.live_check(root, config, name) for name in [args.executor or config.executor]]
    for check in checks:
        print(check.line(), flush=True)
    return 0 if all(check.passed for check in checks) else 1
