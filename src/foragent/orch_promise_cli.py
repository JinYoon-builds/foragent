"""`fa orch promise add|list|done`: record, list and finish promises (orch_promise)."""

from __future__ import annotations


def add_parsers(orch_actions) -> None:
    from foragent.commands import facade

    cli = facade()
    command = orch_actions.add_parser(
        "promise", help="Record a promise 'when X happens, do Y, owner Z'; the gate tells the owner when it is due")
    actions = command.add_subparsers(dest="promise_action", required=True)
    add = actions.add_parser("add", help="Record a promise and print its id; returns at once")
    add.add_argument("--when", required=True,
                     help="closed:<N>, merged:<N>, approved:<N> or time:<ISO time with a timezone>")
    add.add_argument("--do", required=True, dest="action", help="What the owner does when the event happens")
    add.add_argument("--owner", required=True, help="pm, orchestrator, a lane name or a role name")
    add.add_argument("--limit-minutes", type=int, default=None,
                     help="Minutes a due promise may stay undone before it is late (default 60)")
    listing = actions.add_parser("list", help="List the open promises")
    listing.add_argument("--json", action="store_true")
    done = actions.add_parser("done", help="Mark a promise done; it is removed")
    done.add_argument("id")
    for sub in (add, listing, done):
        sub.add_argument("--repo-root", type=cli.Path, default=cli.Path.cwd())
        sub.set_defaults(handler=run)


def run(args) -> int:
    import sys

    from foragent import orch_promise
    from foragent.commands import facade, repo_store

    cli = facade()
    config, repo, store = repo_store(args.repo_root.resolve())
    now = cli.datetime.now(cli.UTC)
    try:
        if args.promise_action == "add":
            record = orch_promise.add(store, cli.GhTracker(repo), args.when, args.action, args.owner, now,
                                      args.limit_minutes, known_owners=orch_promise.owners(config))
            print(f"Recorded promise {record['id']}: when {record['when']}, {record['owner']} does: {record['do']}")
        elif args.promise_action == "done":
            record = orch_promise.done(store, args.id.strip(), now)
            print(f"Promise {record['id']} done and removed.")
        elif args.json:
            print(cli.json.dumps(orch_promise.promises(store), ensure_ascii=False))
        else:
            rows = orch_promise.promises(store)
            if not rows:
                print("No open promises.")
            for row in rows:
                print(f"{row['id']} [{row['state']}] {row['owner']}: when {row['when']}, {row['do']}")
    except orch_promise.PromiseError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    return 0
