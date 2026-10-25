"""`fa ci workflow`: write or replace the generated acceptance comparison workflow."""

from foragent.commands import facade

ORDER = 55


def add(commands) -> None:
    cli = facade()
    ci = commands.add_parser("ci", help="Acceptance comparison workflow")
    ci_actions = ci.add_subparsers(dest="action", required=True)
    workflow = ci_actions.add_parser("workflow", help="Write or replace the generated workflow")
    workflow.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def run(args) -> int:
    from foragent.ci_workflow import write_workflow
    print(f"Wrote {write_workflow(args.repo_root.resolve())}")
    return 0
