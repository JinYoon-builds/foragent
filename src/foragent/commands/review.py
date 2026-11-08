"""`fa review`: print an independent review of a local branch or worktree."""

from foragent.commands import facade

ORDER = 146
NOTHING_TO_REVIEW = 3  # neither PASS (0), FAIL (1) nor an error (2)


def add(commands) -> None:
    cli = facade()
    parser = commands.add_parser("review", help=cli.text("branch_review_help"),
                                 description=cli.text("branch_review_description"))
    parser.add_argument("target", help=cli.text("branch_review_target_help"))
    parser.add_argument("--base", default="main", help=cli.text("branch_review_base_help"))
    context = parser.add_mutually_exclusive_group()
    context.add_argument("--goal", help=cli.text("branch_review_goal_help"))
    context.add_argument("--issue", type=int, help=cli.text("branch_review_issue_help"))


def run(args) -> int:
    from foragent.review_branch import NothingToReview, ReviewError, review
    from foragent.run_attempt import ExecutorStopped

    cli = facade()

    shown = {"language": "en"}  # Reviewers start after the start line, which names the language.

    def started(language: str, fork: str, count: int) -> None:
        shown["language"] = language
        print(cli.text("branch_review_start", language, target=args.target, base=args.base,
                       fork=fork[:12], count=count), flush=True)

    import signal

    from foragent.processes import ReviewStopped, stop_scope

    def stop(signum, frame):
        # None: a second signal while this thread runs the stop; the stop goes on.
        if (error := scope.interrupt(signum)) is not None:
            raise error

    try:
        with stop_scope() as scope:
            previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
            try:
                result, language, worktree = review(args.target, args.base, goal=args.goal, issue=args.issue,
                                                    on_start=started)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except ReviewStopped as exc:
        language = shown["language"]
        print(cli.text("branch_review_stopped", language, signal=signal.Signals(exc.signum).name),
              file=cli.sys.stderr)
        # The scope's list, not the exception's copy: stops of late records add to it.
        for pid, reason in dict.fromkeys(scope.left):
            key = "branch_review_left" if pid is not None else "branch_review_left_unverified"
            print(cli.text(key, language, pid=pid, reason=reason), file=cli.sys.stderr)
        return 128 + exc.signum
    except NothingToReview as exc:
        print(cli.text("branch_review_nothing", exc.args[0], target=args.target, base=args.base))
        return NOTHING_TO_REVIEW
    except (ReviewError, cli.GitError, cli.ConfigError, ValueError, OSError, cli.subprocess.SubprocessError,
            ExecutorStopped) as exc:
        detail = " ".join(" ".join((str(exc), *getattr(exc, "__notes__", ()))).split())
        print(cli.text("branch_review_error", error=detail), file=cli.sys.stderr)
        return 2
    if worktree:
        print(cli.text("branch_review_head_only", language))
    print(cli.text("branch_review_verdict", language, verdict=result.verdict))
    if result.findings:
        print(result.findings)
    for note in result.review_notes:
        print(cli.text("branch_review_note", language, note=note))
    for conflict in result.conflicts:
        print(conflict)
    if result.verdict == "UNUSABLE":
        for note in result.notes:
            print(cli.text("branch_review_note", language, note=note))
    if result.conflicts or result.execution_failed:
        return 2
    return {"PASS": 0, "FAIL": 1}.get(result.verdict, 2)
