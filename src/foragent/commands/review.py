"""`fa review`: print an independent review of a local branch or worktree."""

from foragent.commands import facade

ORDER = 146


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
    from foragent.review_branch import ReviewError, review
    from foragent.run_attempt import ExecutorStopped

    cli = facade()
    try:
        result, language, worktree = review(args.target, args.base, goal=args.goal, issue=args.issue)
    except (ReviewError, cli.GitError, cli.ConfigError, cli.TrackerError, ValueError, OSError, cli.subprocess.SubprocessError,
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
