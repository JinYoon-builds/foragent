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
    doctor.add_argument("--setup", action="store_true", help="Read the machine setup checklist (works before init)")
    doctor.add_argument("--json", action="store_true", help="Print the setup checklist as one JSON object")
    doctor.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    if args.setup:
        from foragent import setup

        def read_auth():
            result = cli.gh.run(["auth", "status"], capture_output=True, text=True, check=True, timeout=10)
            return result.stdout + "\n" + result.stderr

        try:
            language = cli.load_config(args.repo_root).language
        except (cli.ConfigError, OSError):
            language = "en"
        items = setup.checklist(setup.collect(read_auth, language=language))
        if args.json:
            print(cli.json.dumps({"items": items}))
        else:
            for item in items:
                print(f"{'PASS' if item['ok'] else 'FAIL'} {item['id']}" +
                      (f": {item['fix']}" if not item['ok'] else ""))
        return 0 if all(item["ok"] for item in items) else 1
    root = args.repo_root.resolve()
    if not (root / ".foragent.yml").is_file():
        print("foragent: not a Foragent repo: .foragent.yml missing; run `fa init` first", file=cli.sys.stderr)
        return 1
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
