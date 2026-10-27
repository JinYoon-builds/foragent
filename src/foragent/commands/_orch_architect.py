"""`fa orch architect --why` and its hidden session verb `fa gate run-architect`.

Discovery skips this module because its name starts with an underscore.
"""

from foragent.commands import facade, repo_store


def add_orch(orch_actions) -> None:
    cli = facade()
    command = orch_actions.add_parser("architect", help=cli.text("architect_help"))
    command.add_argument("--why", required=True, help=cli.text("architect_why_help"))
    command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    command.set_defaults(handler=run_orch)


class _HiddenChoices(dict):
    """Subcommands that dispatch but never appear in usage, help or invalid-choice errors.

    argparse lists choices by iterating this mapping and dispatches by key lookup,
    so iteration skips the hidden names while ``in`` and ``[]`` still find them.
    """

    hidden = frozenset({"run-architect"})

    def __iter__(self):
        return (name for name in super().__iter__() if name not in self.hidden)


def add_gate(gate_actions) -> None:
    cli = facade()
    # No help= keeps it out of the help body; the mapping hides it from usage and errors.
    gate_actions._name_parser_map = gate_actions.choices = _HiddenChoices(gate_actions.choices)
    command = gate_actions.add_parser("run-architect")
    command.add_argument("--repo-root", type=cli.Path, required=True)
    command.add_argument("--started-at", required=True)
    command.add_argument("--why", required=True)
    command.set_defaults(handler=run_gate)


def run_orch(args) -> int:
    from foragent import orch_architect

    cli = facade()
    root = args.repo_root.resolve()
    config, _, store = repo_store(root)
    try:
        print(orch_architect.call(root, store, config, args.why, cli.datetime.now(cli.UTC)))
    except orch_architect.ArchitectRefused as exc:
        print(f"foragent: {exc}", file=cli.sys.stderr)
        return 1
    return 0


def run_gate(args) -> int:
    from foragent import orch_architect

    cli = facade()
    root = args.repo_root.resolve()
    config, _, store = repo_store(root)
    return orch_architect.run_session(root, store, config, args.why, args.started_at,
                                      lambda: cli.datetime.now(cli.UTC))
