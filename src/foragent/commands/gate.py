"""`fa gate`: schedule and inspect approved issues."""

from dataclasses import asdict
from pathlib import Path
import os
import sys
from types import SimpleNamespace
from typing import Any

from foragent.commands import facade

ORDER = 110
LAUNCHD_ACTIONS = {"install", "uninstall", "launchd-status"}


def add(commands) -> None:
    gate = commands.add_parser("gate", help="Schedule and inspect approved issues")
    actions = gate.add_subparsers(dest="action", required=True)
    for name in ("tick", "status"):
        command = actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")
        if name == "tick":
            command.add_argument("--no-start", action="store_true")
        else:
            command.add_argument("--live", action="store_true",
                                 help="Read GitHub once and refresh the local snapshot first")
    watch_command = actions.add_parser("watch", help="Watch the lane conveyor")
    watch_command.add_argument("--repo-root", type=Path, default=Path.cwd())
    watch_command.add_argument("--once", action="store_true")
    watch_command.add_argument("--open", action="store_true", help="Open the view in a split or new window and return")
    run_issue = actions.add_parser("run-issue", help="Run one approved issue")
    run_issue.add_argument("number", type=int)
    run_issue.add_argument("--repo-root", type=Path, required=True)
    done = actions.add_parser("check-done", help="Check Done when for a native Stop hook")
    done.add_argument("--snapshot", type=Path, required=True)
    done.add_argument("--worktree", type=Path, required=True)
    done.add_argument("--acceptance-dir", required=True)
    for name in ("install", "uninstall", "launchd-status"):
        command = actions.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())


def _status(tracker, store, config=None) -> dict[str, Any]:
    """Local status; the tracker is accepted for compatibility and never read."""
    cli = facade()
    return cli.collect_status(store, config or cli.Config(), cli.datetime.now(cli.UTC))


def _refresh_status(tracker, store, config) -> None:
    """Read GitHub once for --live; a failure is recorded and the stale snapshot is shown."""
    cli = facade()
    context = SimpleNamespace(store=store, config=config, now=lambda: cli.datetime.now(cli.UTC))
    try:
        cli.RecordingTracker(tracker, context).refresh_issues()
    except cli.TrackerError:
        pass


def _print_status(status: dict[str, Any], language: str = "en") -> None:
    cli = facade()
    text = cli.text
    if cli.status_unknown(status):
        print(text("status_unknown", language) + (f": {status['error']}" if status.get("error") else ""))
    print(text("summary", language, **cli.summary_values(status["summary"])))
    if status.get("as_of"):
        print(text("status_as_of", language, time=status["as_of"]))
    print(f"launchd: {'loaded' if status['launchd']['loaded'] else 'not loaded'}")
    for lane in status["lanes"].values():
        details = [text(lane["state"], language)]
        if lane["issue"] is not None:
            details.append(f"#{lane['issue']} {lane['title']}")
        if lane["phase"]:
            details.append(text(lane["phase"], language))
        for field, key in (("elapsed_seconds", "elapsed"), ("last_activity_seconds", "activity")):
            if lane[field] is not None:
                details.append(text(key, language, seconds=int(lane[field])))
        if lane["waiting_for"]:
            details.append(text("waiting_for", language, issues=", ".join(f"#{n}" for n in lane["waiting_for"])))
        print(f"{lane['lane_display']}: {' · '.join(details)}")
    for state, issues in status["issues"].items():
        print(f"{state}:")
        if state in {cli.RUNNING, cli.READY}:
            for lane, grouped in status["lanes"].items():
                if grouped[state]:
                    print(f"  {grouped['lane_display']}:")
                    for issue in grouped[state]:
                        print(f"    #{issue['number']} {issue['title']}")
            continue
        for issue in issues:
            reason = f" [{issue['reason']}]" if issue.get("reason") else ""
            print(f"  #{issue['number']} {issue['title']}{reason}")
    print("Active runs:")
    for run in status["active_runs"]:
        print(f"  #{run['number']} {run['branch']} pid={run['pid']}")
    print("History (last 10):")
    for run in status["history"]:
        print(f"  #{run['number']} {run['outcome']} started={run['started_at']}")


def _launchd(args, root: Path) -> int:
    launchd = facade().launchd
    if args.action == "launchd-status":
        report = launchd.status(root)
        print(f"{report.label}: plist {'present' if report.plist_present else 'missing'}, "
              f"{'loaded' if report.loaded else 'not loaded'}, "
              f"last exit status: {report.last_exit_status if report.last_exit_status is not None else 'unknown'}")
    else:
        label = getattr(launchd, args.action)(root)
        if args.action == "install":
            facade().kick_tick(root)
        print(f"launchd {'installed' if args.action == 'install' else 'uninstalled'}: {label}")
    return 0


def _tick(args, root, git, config, repo, tracker, store) -> None:
    cli = facade()
    # The outcome is recorded under the tick lock so overlapping ticks keep their order.
    report = cli.orch_tick_outcome.run_recorded(store, config, repo, lambda: cli.tick(cli.TickContext(
        repo=repo, repo_root=root, tracker=tracker, store=store, config=config,
        git=git, now=lambda: cli.datetime.now(cli.UTC), launcher=cli.spawn_runner(root, store),
        start_runs=not args.no_start,
        baseline_launcher=cli.start_baseline_process,
    ))) or cli.TickReport(skipped=cli.BUSY_TICK)
    if args.json:
        print(cli.json.dumps(asdict(report)))
    else:
        if report.skipped:
            print(report.skipped)
        for action in report.actions:
            print(action)
    try:
        cli.spawn_notify(root, store)
    except Exception as exc:
        print(f"foragent: inspect notify failed: {' '.join(str(exc).split())}", file=sys.stderr)


def run(args) -> int:
    cli = facade()
    if args.action == "check-done":
        return cli.check_done(args.snapshot.resolve(), args.worktree.resolve(), args.acceptance_dir)
    root = args.repo_root.resolve()
    if args.action in LAUNCHD_ACTIONS:
        return _launchd(args, root)
    git = cli.GitRepo(root)
    config = cli.load_config(root)
    repo = config.repo or git.remote_slug()
    tracker = cli.GhTracker(repo)
    store = cli.StateStore.for_repo(repo)
    if args.action == "tick":
        _tick(args, root, git, config, repo, tracker, store)
    elif args.action == "status":
        if args.live:
            cli._refresh_status(tracker, store, config)
        status = cli._status(tracker, store, config)
        status["launchd"] = asdict(cli.launchd.status(root, repo=repo))
        status["test_slots"] = cli.capacity.effective_test_slots(config.test_slots)
        status["tick_outcome"] = cli.orch_tick_outcome.read(store)
        if args.json:
            print(cli.json.dumps(status))
        else:
            cli._print_status(status, config.language)
    elif args.action == "watch":
        if args.open:
            cli.watch_open.open_view(root, store, config.language)
        else:
            observation = store.issues_observation()
            if observation["as_of"] is None and observation["error"] is None:
                # Never observed: seed the snapshot once; refreshes stay local.
                cli._refresh_status(tracker, store, config)
            cli.watch.run(
                lambda: cli._status(tracker, store, config), tracker, store, config, root,
                once=args.once, now=lambda: cli.datetime.now(cli.UTC),
            )
    else:
        if store.get_run(args.number) is None:
            raise cli.ConfigError(f"no run record for issue #{args.number}")
        store.update_run(args.number, pid=os.getpid(),
                         process_identities=cli.observe_tree(roots=(os.getpid(),)))
        outcome = cli.run_issue(args.number, cli.RunContext(
            repo=repo, repo_root=root, tracker=tracker, store=store,
            config=config, git=git, now=lambda: cli.datetime.now(cli.UTC),
        ))
        return 1 if outcome == "error" else 0
    return 0
