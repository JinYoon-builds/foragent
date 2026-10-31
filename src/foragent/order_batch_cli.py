"""`fa order note|pending|flush` and `publish --defer`: the CLI side of batched publication (D85)."""

from foragent.commands import facade


def help_language(root) -> str:
    from foragent.commands.order import _help_language

    return _help_language(root)


def local(args, root) -> int:
    from foragent import order_batch
    from foragent.i18n import text

    cli = facade()
    language = help_language(root)  # the queue stays usable while .foragent.yml is damaged
    say = lambda key, **values: text(key, language, **values)
    if args.action == "note":
        entry = order_batch.note(root, args.kind, args.text, args.defer, language,
                                 replace=args.replace)
        print(cli.json.dumps(entry, ensure_ascii=False) if args.json else
              say("batch_note_waiting", boundary=entry["boundary"], kind=entry["kind"], id=entry["id"]))
        return 0
    shown = order_batch.pending(root)
    if args.json:
        print(cli.json.dumps(shown, ensure_ascii=False))
        return 0
    for row in shown["orders"]:
        print(say("batch_pending_order", **{key: row[key] for key in ("status", "boundary", "id", "title")}))
    for row in shown["notes"]:
        print(say("batch_pending_note", **{key: row[key] for key in ("status", "boundary", "kind", "id", "text")}))
    if not shown["orders"] and not shown["notes"]:
        print(say("batch_pending_empty"))
    return 0


def defer(args, root, config, tracker, repo, plan) -> int:
    from foragent import order_batch
    from foragent.i18n import text

    entry = order_batch.defer(plan, args.defer, root, config, tracker, repo=repo, approve=args.approve,
                              approve_keys=args.approve_keys, approve_numbers=args.approve_numbers)
    count = len(plan["issues"]) + len(plan.get("edits", []))
    approved = count if args.approve else len(args.approve_keys) + len(args.approve_numbers)
    print(text("batch_waiting", config.language, boundary=entry["boundary"], title=plan["title"],
               approved=min(approved, count), count=count))
    return 0


def flush(args, root, config, tracker, repo, git) -> int:
    from foragent import order_batch
    from foragent.i18n import text

    cli = facade()
    result = order_batch.flush(args.boundary, root, config, tracker,
                               repo=repo, git=git)
    say = lambda key, **values: text(key, config.language, **values)
    if args.json:
        print(cli.json.dumps(result, ensure_ascii=False))
    elif not result["orders"] and not result["notes"]:
        print(say("batch_nothing", boundary=result["boundary"]))
    else:
        print(say("batch_flushed", boundary=result["boundary"], orders=len(result["orders"]),
                  notes=len(result["notes"])))
        for identity, parent in result["parents"].items():
            print(say("batch_flushed_order", id=identity, parent=parent))
    if result["orders"]:
        cli._open_lane_view(root)
    return 0
