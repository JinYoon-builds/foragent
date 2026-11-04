"""`fa review`: print an independent review of a local branch or worktree."""

import subprocess
import sys

from foragent.config import ConfigError
from foragent.git import GitError
from foragent.i18n import text
from foragent.review_branch import ReviewError, review
from foragent.run_attempt import ExecutorStopped
from foragent.tracker import TrackerError

ORDER = 146


def add(commands) -> None:
    parser = commands.add_parser("review", help=text("branch_review_help"))
    parser.add_argument("target", help=text("branch_review_target_help"))
    parser.add_argument("--base", default="main", help=text("branch_review_base_help"))
    context = parser.add_mutually_exclusive_group()
    context.add_argument("--goal", help=text("branch_review_goal_help"))
    context.add_argument("--issue", type=int, help=text("branch_review_issue_help"))


def run(args) -> int:
    try:
        result, language, worktree = review(args.target, args.base, goal=args.goal, issue=args.issue)
    except (ReviewError, GitError, ConfigError, TrackerError, ValueError, OSError, subprocess.SubprocessError,
            ExecutorStopped) as exc:
        print(text("branch_review_error", error=" ".join(str(exc).split())), file=sys.stderr)
        return 2
    if worktree:
        print(text("branch_review_head_only", language))
    print(text("branch_review_verdict", language, verdict=result.verdict))
    if result.findings:
        print(result.findings)
    for note in result.review_notes:
        print(text("branch_review_note", language, note=note))
    for conflict in result.conflicts:
        print(conflict)
    if result.verdict == "UNUSABLE":
        for note in result.notes:
            print(text("branch_review_note", language, note=note))
    if result.conflicts or result.execution_failed:
        return 2
    return {"PASS": 0, "FAIL": 1}.get(result.verdict, 2)
