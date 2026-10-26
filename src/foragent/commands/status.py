"""`fa status`: this repository's gate status, then every gate installed on this machine."""

from foragent.commands import facade

ORDER = 105


def add(commands) -> None:
    cli = facade()
    command = commands.add_parser("status", help=cli.text("status_help", "en"))
    command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    command.add_argument("--json", action="store_true")
    command.add_argument("--live", action="store_true",
                         help="Read GitHub once and refresh the local snapshot first")


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    if (root / ".foragent.yml").is_file():
        # Inside a repository: fa gate status, which ends with the machine gate list.
        return cli.commands.module("gate").run(cli.SimpleNamespace(
            action="status", repo_root=root, json=args.json, live=args.live))
    # Outside a repository only the machine gate list is known.
    language = cli.load_global_config().language
    rows = cli.gates.collect(cli.datetime.now(cli.UTC))
    if args.json:
        print(cli.json.dumps({"installed_gates": rows}))
    else:
        cli._print_gates(rows, language)
    return 0
