"""The local request and question box under `fa orch`; split from commands/orch.py.

Discovery skips this module because its name starts with an underscore.
"""

from foragent.commands import facade


def _add_inbox(orch_actions) -> None:
    cli = facade()
    request = orch_actions.add_parser("request", help="Put a planning request in the durable box")
    request.add_argument("--from", dest="source", required=True, help="Sending session, e.g. planning")
    request.add_argument("--text", required=True)
    inbox = orch_actions.add_parser("inbox", help="List requests in the box (read this first on start)")
    inbox.add_argument("--pending", action="store_true", help="Hide requests marked done")
    request_status = orch_actions.add_parser("request-status", help="Mark a request processing or done")
    request_status.add_argument("id")
    request_status.add_argument("status", choices=cli.orch_inbox.REQUEST_STATUSES)
    request_status.add_argument("--note")
    ask = orch_actions.add_parser("ask", help="Record a D71 human question in the box")
    ask.add_argument("--question", required=True)
    ask.add_argument("--option", action="append", default=[])
    ask.add_argument("--recommend", type=int)
    ask.add_argument("--kind", choices=cli.orch_inbox.QUESTION_KINDS, default="question",
                     help=cli.text("orch_ask_kind_help"))
    ask.add_argument("--json", action="store_true", help=cli.text("orch_ask_json_help"))
    ask.add_argument("--issue", dest="issues", type=int, action="append", default=[],
                     help="Issue that waits on this answer (repeatable); shown in the board's Your turn")
    questions = orch_actions.add_parser("questions", help="List open human questions")
    questions.add_argument("--all", action="store_true", help="Include answered questions")
    answer = orch_actions.add_parser("answer", help="Record the answer and close one question")
    answer.add_argument("id")
    answer.add_argument("--text", required=True)
    note = orch_actions.add_parser("note", help="Record what the orchestrator does now, why, next and where")
    note.add_argument("--now")
    note.add_argument("--person-found", dest="person_found",
                      help="Record one problem the person found before the retrospective")
    note.add_argument("--why")
    note.add_argument("--next", dest="next_step")
    note.add_argument("--where-title", "--orca-title", dest="where_title", help="Orca tab title, e.g. orch: <project>")
    note.add_argument("--tmux-pane", help="tmux pane id when not taken from TMUX_PANE")
    for command in (request, inbox, request_status, ask, questions, answer, note):
        command.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        if command in (inbox, questions):
            command.add_argument("--json", action="store_true")
    alive = orch_actions.add_parser("alive", help=cli.text("heartbeats_help"))
    alive.add_argument("--json", action="store_true")
    alive.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    ensure = orch_actions.add_parser("ensure", help=cli.text("orch_ensure_help"))
    ensure.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    queue = orch_actions.add_parser("queue", help="Set, show or clear a lane's or the cross-lane start order")
    queue.add_argument("lane", nargs="?", help="Lane name; with --global, the first issue number")
    queue.add_argument("--global", dest="global_", action="store_true",
                       help="The cross-lane priority list instead of one lane's queue")
    queue.add_argument("numbers", type=int, nargs="*", help="Issue numbers in start order; none shows the queue")
    queue.add_argument("--clear", action="store_true")
    queue.add_argument("--json", action="store_true")
    queue.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
    from foragent import orch_plan_cli

    orch_plan_cli.add_parsers(orch_actions, queue)  # pause, resume, plan and every verb's --by
    cli.orch_facts.add_parser(orch_actions)
    from foragent import orch_scorecard

    orch_scorecard.add_parser(orch_actions)
    from foragent import orch_roles

    orch_roles.add_parser(orch_actions)
    from foragent import orch_role_raise

    orch_role_raise.add_parser(orch_actions)
    from foragent import orch_refresh

    orch_refresh.add_parsers(orch_actions)
    reviewers = orch_actions.add_parser("reviewers", help="Set or clear how many reviewers check one issue's risk-map PRs")
    reviewers.add_argument("issue", type=int)
    reviewers.add_argument("count", type=int, nargs="?", help="Reviewers running at once (1 or more); wins over review_reviewers")
    reviewers.add_argument("--clear", action="store_true")
    reviewers.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())


def _reviewers(args, store, config) -> None:
    """Per-issue reviewer count in the state directory; .foragent.yml is never written."""
    from foragent import orch_reviewers
    from foragent.i18n import text

    language = config.language
    if args.clear == (args.count is not None):
        raise SystemExit(text("orch_reviewers_usage", language))
    if args.clear:
        orch_reviewers.clear(store, args.issue)
        print(text("orch_reviewers_cleared", language, issue=args.issue,
                   count=orch_reviewers.effective(store, config, args.issue)))
        return
    if args.count < 1:
        raise SystemExit(text("orch_reviewers_invalid", language, count=args.count,
                              current=orch_reviewers.effective(store, config, args.issue)))
    orch_reviewers.set_count(store, args.issue, args.count)
    print(text("orch_reviewers_set", language, issue=args.issue, count=args.count))


def _global_queue(args, store, config) -> None:
    """Cross-lane priority list in the state directory; argparse puts the first number in ``lane``."""
    cli = facade()
    text, language = cli.text, config.language
    numbers = list(args.numbers)
    if args.lane is not None:
        try:
            numbers.insert(0, int(args.lane))
        except ValueError:
            raise SystemExit(text("orch_queue_global_number", language, value=args.lane)) from None
    if args.clear and numbers:
        raise SystemExit(text("orch_queue_global_usage", language))
    order = cli.orch_queue.set_global(store, []) if args.clear else (
        cli.orch_queue.set_global(store, numbers) if numbers else cli.orch_queue.get_global(store))
    if args.json:
        print(cli.json.dumps({"lane": None, "order": order}))
    elif order:
        print(text("orch_queue_global_show", language, order=" ".join(f"#{n}" for n in order)))
    else:
        print(text("orch_queue_global_empty", language))


def _orch_inbox(args, store, config, repo: str) -> None:
    """Local request/question box; never reads GitHub or starts an executor."""
    cli = facade()
    box = cli.orch_inbox
    if args.action == "reviewers":
        _reviewers(args, store, config)
        return
    if args.action == "alive":
        _alive(args, store, config)
        return
    if args.action == "facts":
        return cli.orch_facts.command(args, store, config)
    if args.action == "scorecard":
        from foragent import orch_scorecard

        return orch_scorecard.command(args, store, config)
    if args.action in ("roles", "role-decision"):
        from foragent import orch_roles

        return orch_roles.command(args, store, config)
    if args.action == "signal-answer":
        from foragent import orch_role_raise

        return orch_role_raise.command(args, store)
    if args.action == "ensure":
        _ensure(args, store, config)
        return
    from foragent import orch_refresh

    if args.action in orch_refresh.ACTIONS:
        return orch_refresh.command(args, store, config, cli.datetime.now(cli.UTC))
    from foragent import orch_plan_cli

    if args.action in orch_plan_cli.ACTIONS:
        orch_plan_cli.run(args, store, config)
        return
    if args.action == "queue":
        if args.global_:
            _global_queue(args, store, config)
            return
        orch_plan_cli.lane_queue(args, store, config)
        return
    if args.action == "request":
        record = box.add_request(store, args.source, args.text)
        print(f"Request {record['id']} saved ({record['status']}).")
    elif args.action == "request-status":
        record = box.set_request_status(store, args.id, args.status, args.note)
        print(f"Request {record['id']}: {record['status']}.")
    elif args.action == "note":
        if args.person_found is not None:
            event = box.person_found(store, args.person_found)
            print(f"Person-found event recorded at {event['at']}.")
        if args.now is not None or args.person_found is None:
            where = box.note_location(dict(cli.os.environ), args.where_title, args.tmux_pane)
            record = box.write_note(store, args.now or "", args.why, args.next_step, where)
            print(f"Note saved at {record['at']} ({where['kind']}).")
    elif args.action == "ask":
        record = box.ask(store, args.question, args.option, args.recommend, args.issues, args.kind)
        pushed = box.route_question(store, config, repo, record)
        _print_ask(args, record, pushed, config.language)
    elif args.action == "answer":
        record = box.answer(store, args.id, args.text)
        print(f"Question {record['id']} closed.")
    elif args.action == "inbox":
        rows = box.requests(store, pending=args.pending)
        if args.json:
            print(cli.json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("No requests.")
        for row in [] if args.json else rows:
            print(f"{row['id']} [{row['status']}] from {row['from']} · {row['created_at']}")
            print(row["text"])
            for note in row.get("notes", []):
                print(f"  {note['at']} {note['status']}: {note['note']}")
    else:
        rows = box.questions(store, include_closed=args.all)
        if args.json:
            print(cli.json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print("No open questions.")
        for row in [] if args.json else rows:
            print(f"{row['id']} [{row['status']}] {row['question']}")
            if row.get("issues"):
                print("  issues: " + ", ".join(f"#{number}" for number in row["issues"]))
            for index, option in enumerate(row["options"], 1):
                mark = " (recommended)" if index == row["recommend"] else ""
                print(f"  {index}. {option}{mark}")
            if row.get("answer"):
                print(f"  answer: {row['answer']}")


def _print_ask(args, record, pushed: bool, language: str) -> None:
    """With planning alive, the handoff the agent sends by SendMessage (#970); else the push note."""
    cli = facade()
    fields = {key: record[key] for key in ("id", "kind", "question", "options", "recommend")}
    if args.json:
        print(cli.json.dumps({**fields, "route": "push" if pushed else "planning"}, ensure_ascii=False))
        return
    if pushed:
        print(cli.text("orch_ask_pushed", language, **fields))
        return
    print(cli.text("orch_ask_planning", language, **fields))
    print(record["question"])
    for index, option in enumerate(record["options"], 1):
        mark = cli.text("orch_ask_recommended", language) if index == record["recommend"] else ""
        print(f"  {index}. {option}{mark}")


def _alive(args, store, config) -> None:
    from foragent.commands.orch import ALIVE_SECONDS

    cli = facade()
    last = store.heartbeats()["orchestrator"]
    elapsed = None if last is None else (store._now() - last).total_seconds()
    # Compare the exact elapsed time; the whole seconds below are only for display.
    alive = elapsed is not None and elapsed <= ALIVE_SECONDS
    age = None if elapsed is None else int(elapsed)
    if args.json:
        print(cli.json.dumps({"alive": alive, "last_seen": last and last.isoformat(), "age_seconds": age}))
    elif last is None:
        print(cli.text("heartbeats_never", config.language))
    else:
        key = "heartbeats_alive" if alive else "heartbeats_gone"
        print(cli.text(key, config.language, age=age, at=last.isoformat()))


def _ensure(args, store, config) -> None:
    """Explicit planning-session call (D76): the gate's spawn without its spacing; one line either way.

    The reason is read before the call, so a live orchestrator never reaches the
    spawn; a call that opened nothing reports why from inside its lock.
    """
    cli = facade()
    from foragent import orch_ensure, orch_spawn

    root = args.repo_root.resolve()
    now = cli.datetime.now(cli.UTC)
    reason = orch_ensure.skip_reason(root, store, now)
    if reason is not None:  # The gate's silence step: rearm, or the hourly note and the #952 push.
        orch_spawn.observe_silence(root, store, config, now, reason)
    if reason is None:
        why = []
        screen = orch_spawn.ensure_orchestrator(root, store, config, now=now, spacing=False, why=why)
        if screen:
            print(cli.text("orch_ensure_opened", config.language, screen=screen))
            return
        reason = why[-1] if why else "failed"
    print(cli.text(f"orch_ensure_{reason}", config.language))
