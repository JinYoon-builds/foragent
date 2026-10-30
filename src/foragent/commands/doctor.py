"""`fa doctor`: report role and lane drift; with --live, check that the executors can do real work."""

from foragent.commands import facade

ORDER = 40


def add(commands) -> None:
    cli = facade()
    doctor = commands.add_parser("doctor", help="Check role and lane drift; --live also checks the executors")
    doctor.add_argument("--live", action="store_true",
                        help="Run each executor once on a throwaway task in a temporary clone")
    doctor.add_argument("--executor", choices=("codex", "claude"),
                        help="Executor to check (default: the configured executor)")
    doctor.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    config = cli.load_config(root)
    from foragent.roles import role_drift
    from foragent.runner_doctor import lane_skill_drift

    drift = [*role_drift(root, config), *lane_skill_drift(root, config)]
    for line in drift:
        print(line, flush=True)
    if not args.live:
        print("PASS roles and lanes match .foragent.yml" if not drift else
              "Skipped executor checks; run `fa doctor --live` to run them.", flush=True)
    checks = [cli.live_check(root, config, name) for name in [args.executor or config.executor]] if args.live else []
    for check in checks:
        print(check.line(), flush=True)
    return 0 if not drift and all(check.passed for check in checks) else 1
