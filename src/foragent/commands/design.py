"""`fa design`: design previews and the project's foundation check."""

from foragent.commands import facade

ORDER = 10


def add(commands) -> None:
    cli = facade()
    design_command = commands.add_parser("design", help="Preview designs and check the project's foundation")
    design_actions = design_command.add_subparsers(dest="action", required=True,
                                                  metavar="{lint,preview,picks,done}")
    lint = design_actions.add_parser("lint", help="Report raw colors outside token files")
    lint.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    lint.add_argument("--tokens", nargs="+", action="extend", help="Token paths/globs (replaces defaults)")
    lint.add_argument("--glob", nargs="+", action="extend", dest="globs", help="UI globs (replaces defaults)")
    lint.add_argument("--json", action="store_true")
    preview = design_actions.add_parser("preview", help="Show references or running drafts in planning mode")
    preview.add_argument("--stage", choices=("board", "drafts"), default="drafts")
    preview.add_argument("--topic", default="default")
    preview.add_argument("--references", type=cli.Path, help="JSON reference manifest for the local board")
    medium = preview.add_mutually_exclusive_group()
    medium.add_argument("--command", dest="preview_commands", action="append", help="Runnable terminal draft; repeat to compare")
    medium.add_argument("--url", action="append", help="Running web draft; repeat to compare")
    preview.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    picks = design_actions.add_parser("picks", help="Read saved reference likes and dislikes")
    picks.add_argument("--topic", required=True)
    picks.add_argument("--json", action="store_true")
    picks.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    done = design_actions.add_parser("done", help="Close preview panes and return to the lane view")
    done.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    serve = design_actions.add_parser("_serve")
    serve.add_argument("--directory", type=cli.Path, required=True)
    serve.add_argument("--topic", required=True)
    serve.add_argument("--token", required=True)
    serve.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    cli = facade()
    if args.action == "lint":
        return cli.design.run(args)
    root = args.repo_root.resolve()
    config = cli.load_config(root)
    repo = config.repo or cli.GitRepo(root).remote_slug()
    return cli.design_session.run(args, cli.StateStore.for_repo(repo))
