"""`fa decisions`: decision links and derived completion from local facts only (#1025)."""

from foragent.commands import facade, repo_store

ORDER = 142


def add(commands) -> None:
    from foragent.commands.order import _help_language

    cli = facade()
    language = _help_language()
    command = commands.add_parser("decisions", help=cli.text("decisions_help", language))
    command.add_argument("--json", action="store_true", help=cli.text("decisions_json_help", language))
    command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    actions = command.add_subparsers(dest="decisions_action")
    # Subcommand options keep SUPPRESS defaults so they never overwrite a value given before the action.
    sweep = actions.add_parser("_sweep", help=cli.argparse.SUPPRESS)
    sweep.add_argument("--repo-root", type=cli.Path, default=cli.argparse.SUPPRESS)
    confirm = actions.add_parser("add", help=cli.text("decision_sweep_add_help", language))
    confirm.add_argument("candidate", help=cli.text("decision_sweep_candidate_help", language))
    confirm.add_argument("--text", required=True, help=cli.text("decision_sweep_text_help", language))
    confirm.add_argument("--defer", required=True, help=cli.text("order_notes_defer_help", language))
    confirm.add_argument("--repo-root", type=cli.Path, default=cli.argparse.SUPPRESS)
    confirm.add_argument("--json", action="store_true", default=cli.argparse.SUPPRESS)
    stop = actions.add_parser("stop-check", help=cli.text("decisions_stop_help", language))
    stop.add_argument("--repo-root", type=cli.Path, default=cli.argparse.SUPPRESS)
    drop = actions.add_parser("drop", help=cli.text("decisions_drop_help", language))
    drop.add_argument("identity")
    drop.add_argument("--why", required=True, help=cli.text("decisions_why_help", language))
    drop.add_argument("--repo-root", type=cli.Path, default=cli.argparse.SUPPRESS)


def run(args) -> int:
    from foragent import decisions

    cli = facade()
    if args.decisions_action == "_sweep":
        from foragent import decision_sweep
        return decision_sweep.hook(args.repo_root)
    if args.decisions_action == "stop-check":
        return _stop_check()
    root = args.repo_root.resolve()
    config, _repo, store = repo_store(root)
    if args.decisions_action == "add":
        if "FORAGENT_ROLE" in cli.os.environ:
            print(cli.text("launched_person_only", config.language, command="decisions add"), file=cli.sys.stderr)
            return 1
        from foragent import decision_sweep
        result = decision_sweep.confirm(store, args.candidate, args.text, args.defer, config.language)
        print(cli.json.dumps(result, ensure_ascii=False) if args.json else
              cli.text("order_notes_saved", config.language, id=result["id"], boundary=result["boundary"]))
        return 0
    ledger = root / decisions.LEDGER
    if args.decisions_action == "drop":
        try:
            decisions.drop(root, args.identity, args.why)
        except (OSError, UnicodeError) as exc:
            print(cli.text("decisions_write_failed", config.language, error=exc), file=cli.sys.stderr)
            return 1
        except ValueError as exc:
            print(cli.text(str(exc), config.language, id=args.identity), file=cli.sys.stderr)
            return 1
        print(cli.text("decisions_dropped", config.language, id=args.identity, why=args.why.strip()))
        return 0
    try:
        text = ledger.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        print("foragent: " + cli.text("decisions_ledger_missing", config.language, path=ledger), file=cli.sys.stderr)
        return 1
    rows = decisions.view(root, decisions.observed_rows(store._issue_view()), text)
    if args.json:
        print(cli.json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print(cli.text("decisions_none", config.language))
    for row in rows:
        issues = ", ".join(f"#{n}" for n in row["issues"]) or cli.text("decisions_no_issues", config.language)
        print(cli.text("decisions_row", config.language, id=row["id"], state=row["state"], issues=issues))
        if row["state"] == "dropped":
            print(cli.text("decisions_drop_reason", config.language, why=row["why"]))
    return 0


def _stop_check() -> int:
    from foragent import decisions, presence

    cli = facade()
    if "FORAGENT_ROLE" in cli.os.environ:
        return 0
    language = "en"
    try:
        payload = cli.json.load(cli.sys.stdin)
        if not isinstance(payload, dict) or payload.get("stop_hook_active"):
            return 0
        root = presence.project_root(cli.Path(payload.get("cwd") or cli.Path.cwd()))
        if root is None:
            return 0
        config, _repo, store = repo_store(root)
        language = config.language
        ledger = (root / decisions.LEDGER).read_text(encoding="utf-8")
        rows = decisions.view(root, decisions.observed_rows(store._issue_view()), ledger)
        count, oldest = decisions.backlog(root, rows, ledger)
    except Exception as exc:
        print(cli.text("decisions_check_failed", language, error=exc), file=cli.sys.stderr)
        return 1
    if count >= 4 or oldest > 50:
        print(cli.json.dumps({"decision": "block", "reason": cli.text(
            "decisions_stop_reason", language, count=count, days=oldest)}, ensure_ascii=False))
    return 0


def install_stop_hook(root) -> None:
    """Explicit project opt-in; preserve other settings and keep a single copy."""
    from foragent import statusline

    cli = facade()
    language = cli.load_config(root).language
    path = root / ".claude/settings.local.json"
    try:
        with statusline._install_lock():
            settings = cli.json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            hooks = settings.setdefault("hooks", {})
            groups = hooks.setdefault("Stop", [])
            command = "fa decisions stop-check"
            if not any(statusline._fa_arguments(hook.get("command", "")) == ["decisions", "stop-check"]
                       for group in groups for hook in group.get("hooks", []) if hook.get("type") == "command"):
                groups.append({"hooks": [{"type": "command", "command": command, "timeout": 15}]})
                statusline._write_json(path, settings)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(cli.text("decisions_hook_failed", language, error=exc), file=cli.sys.stderr)
        return
    print(cli.text("decisions_hook_installed", language))
