"""`fa inspect`: the human inbox and recorded answers."""

import argparse
import json
from pathlib import Path

from foragent.commands import facade

ORDER = 180


def add(commands) -> None:
    inspect = commands.add_parser("inspect", help="Show the human inbox and record answers")
    inspect.add_argument("--repo-root", type=Path, default=Path.cwd())
    inspect.add_argument("--json", action="store_true")
    answers = inspect.add_subparsers(dest="action")
    for name in ("answer", "retry", "feedback", "mark-seen", "notify", "prepare", "serve", "stop"):
        command = answers.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
        if name in {"answer", "retry", "serve"}:
            command.add_argument("number", type=int)
        if name == "answer":
            command.add_argument("--text", required=True)
        if name == "feedback":
            command.add_argument("--title", required=True)
            command.add_argument("--body", required=True)
            command.add_argument("--about", type=int)


def _inspect(args: argparse.Namespace, ctx) -> None:
    cli = facade()
    actions, data = cli.inspect_actions, cli.inspect_data
    if args.action == "prepare":
        numbers = actions.prepare(ctx)
        print("Prepared: " + ", ".join(f"#{number}" for number in numbers) if numbers else "No new results to prepare.")
    elif args.action == "serve":
        for url in actions.serve(ctx, args.number):
            print(url)
    elif args.action == "stop":
        actions.stop(ctx)
        print("Served apps stopped.")
    elif args.action == "answer":
        comment = actions.answer(ctx, args.number, args.text)
        cli.kick_tick(ctx.repo_root)
        print(f"Decision recorded: https://github.com/{ctx.repo}/issues/{args.number}#issuecomment-{comment}")
    elif args.action == "retry":
        actions.retry(ctx, args.number)
        cli.kick_tick(ctx.repo_root)
        print(f"Retry requested for #{args.number}.")
    elif args.action == "feedback":
        number = actions.feedback(ctx, args.title, args.body, args.about)
        print(f"Feedback: https://github.com/{ctx.repo}/issues/{number}")
    elif args.action == "mark-seen":
        print(f"Seen through {actions.mark_seen(ctx)}")
    elif args.action == "notify":
        if line := actions.notify(ctx):
            print(line)
    else:
        inbox = data.collect(ctx)
        if args.json:
            print(json.dumps(data.inbox_payload(inbox)))
        else:
            print(data.counts_line(inbox, detailed=True))
            for item in inbox.items:
                summary = f"[{item.group}] {item.lane_display} · #{item.number} {item.title} — {item.summary}"
                if item.group == "try":
                    print(f"{summary} {item.url}")
                    for line in data.try_lines(item):
                        print("  " + line.replace("\n", "\n  "))
                    continue
                print(summary)
                for line in item.details.splitlines()[:12]:
                    print(f"  {line}")
                print(f"  {item.url}")


def run(args) -> int:
    cli = facade()
    root = args.repo_root.resolve()
    git = cli.GitRepo(root)
    config = cli.load_config(root)
    repo = config.repo or git.remote_slug()
    tracker = cli.GhTracker(repo)
    store = cli.StateStore.for_repo(repo)
    ctx = cli.inspect_data.InspectContext(
        tracker, store, repo, config, lambda: cli.datetime.now(cli.UTC), repo_root=root, git=git,
    )
    cli._inspect(args, ctx)
    return 0
