"""`fa risk`: add to, list, and find candidates for the project's risk map."""

from foragent.commands import facade

ORDER = 145


def add(commands) -> None:
    cli = facade()
    risk = commands.add_parser("risk", help="Manage the risk map the reviewer reads (.foragent-risks.yml)")
    actions = risk.add_subparsers(dest="action", required=True)
    add_risk = actions.add_parser("add", help="Append one risk entry; existing entries stay byte for byte")
    add_risk.add_argument("--path", action="append", required=True, help="Repo-relative glob (repeatable)")
    add_risk.add_argument("--why", required=True)
    add_risk.add_argument("--incident-date", required=True, help="YYYY-MM-DD")
    add_risk.add_argument("--incident", required=True, help="What happened")
    add_risk.add_argument("--guidance", required=True, help="What the reviewer must do for these paths")
    add_risk.add_argument("--always-human", action="store_true", help="Hold matching PRs for a person")
    add_risk.add_argument("--event", help="Event that led to this entry")
    add_risk.add_argument("--fixed-by", help="Issue reference of the fix")
    add_risk.add_argument("--guard-test", action="append", default=[], help="Test guarding the incident (repeatable)")
    add_risk.add_argument("--by", help="Who adds the entry (default: the current user)")
    listing = actions.add_parser("list", help="Show the risk map")
    candidates = actions.add_parser("candidates", help="Propose entries from dangerous-pattern code; writes nothing")
    for parser in (add_risk, listing, candidates):
        parser.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    for parser in (listing, candidates):
        parser.add_argument("--json", action="store_true")


def _language(root) -> str:
    cli = facade()
    try:
        return cli.load_config(root).language
    except (cli.ConfigError, OSError):
        return "en"


def _run(args, root, language: str) -> None:
    import getpass

    from foragent import risks

    cli = facade()
    if args.action == "add":
        risk = risks.entry(
            paths=args.path, why=args.why, incident_date=args.incident_date, incident=args.incident,
            guidance=args.guidance, by=args.by if args.by is not None else getpass.getuser(),
            always_human=args.always_human, event=args.event, fixed_by=args.fixed_by,
            guard_tests=args.guard_test, root=root,
        )
        risks.append(root, risk)
        print(cli.text("risk_added", language, paths=", ".join(risk["paths"]), file=risks.RISK_FILE))
        return
    if args.action == "list":
        entries = risks.load(root)
        if args.json:
            print(cli.json.dumps(entries, ensure_ascii=False, default=str))
            return
        if not entries:
            print(cli.text("risk_empty", language, file=risks.RISK_FILE))
        for risk in entries:
            human = cli.text("risk_always_human", language) if risk.get("always_human") else ""
            print(f"- {', '.join(risk['paths'])}{human}: {risk.get('why', '')}")
        return
    files = [name for name in cli.GitRepo(root)._call(["ls-files", "--cached", "-z"]).split("\0") if name]
    report = risks.candidates(root, files)
    if args.json:
        print(cli.json.dumps(report, ensure_ascii=False))
        return
    covered = sum(item["covered"] for item in report["candidates"])
    print(cli.text("risk_candidates", language, found=len(report["candidates"]), covered=covered))
    for proposal in report["proposals"]:
        print(f"- {proposal['paths'][0]}: {', '.join(proposal['patterns'])}")


def run(args) -> int:
    cli = facade()
    from foragent.risks import RiskError

    root = args.repo_root.resolve()
    language = _language(root)
    try:
        _run(args, root, language)
    except RiskError as exc:
        print("foragent: " + cli.text(exc.key, language, **exc.values), file=cli.sys.stderr)
        return 1
    return 0
