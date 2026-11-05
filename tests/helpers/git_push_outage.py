"""The Git remote's push answers for unit tests of push waits (copied from the locked push-outage test)."""

from dataclasses import replace
from pathlib import Path
import subprocess

from conftest import git_command
from foragent.runner import review_only, run_issue
from helpers.github_outage import assert_wait, restart_at_wait_end


BRANCH = "foragent/issue-1"

# The text GitHub returned on 2026-11-04 (runs #1635, #1960 and five more).
INTERNAL = (
    "remote: Internal Server Error        \n"
    "remote: Request ID E89A:1BA268:4F23FF:7E7133:6AEB0225        \n"
    "remote: Time 2026-11-04T10:00:06Z        \n"
    "To https://github.com/owner/repo\n"
    f" ! [remote rejected]   HEAD -> {BRANCH} (Internal Server Error)\n"
    "error: failed to push some refs to 'https://github.com/owner/repo'\n"
)

BUDGET_MINUTES = 10


class Remote:
    """The issue branch on the Git remote. Answers each push with the next scripted
    stderr (``None`` = accept); afterwards it accepts. Leases are honoured like Git."""

    def __init__(self, case, answers, on_failure=lambda: None):
        self.case, self.answers, self.on_failure = case, list(answers), on_failure
        self.tried, self.landed = [], []
        self._original = case.git._run
        case.git._run = self._run

    def head(self):
        return git_command(self.case.history.origin, "rev-parse", f"refs/heads/{BRANCH}")

    def _run(self, args):
        root, rest = self.case.root, list(args)
        while rest[:1] == ["-C"]:
            root, rest = Path(rest[1]), rest[2:]
        if rest[:1] != ["push"] or not rest[-1].endswith(BRANCH):
            return self._original(args)
        sha = git_command(root, "rev-parse", rest[-1].split(":", 1)[0])
        self.tried.append(sha)
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            self.on_failure()
            raise subprocess.CalledProcessError(1, ["git", *args], stderr=answer)
        lease = next((item.rsplit(":", 1)[1] for item in rest if item.startswith("--force-with-lease=")), None)
        if lease is not None:
            if self._current() != lease:
                raise subprocess.CalledProcessError(1, ["git", *args], stderr=(
                    "To https://github.com/owner/repo\n"
                    f" ! [rejected]        HEAD -> {BRANCH} (stale info)\n"
                    "error: failed to push some refs to 'https://github.com/owner/repo'\n"))
        result = self._original(args)
        self.landed.append(sha)
        if (pr := self.case.tracker.find_pr(BRANCH)) is not None:
            self.case.tracker.set_pr_head_sha(pr, sha)  # GitHub moves the PR with its branch.
        return result

    def _current(self):
        try:
            return self.head()
        except Exception:
            return ""


def start(case, entry):
    """Leave the PR head behind main so review-only must refresh (merge + leased push)."""
    case.ctx.config = case.config = replace(case.ctx.config, transient_max_wait_minutes=BUDGET_MINUTES)
    if entry == "review":
        case.history.update({"base-moved.txt": "main moved after the PR was opened\n"}, message="Move main")
    return run_issue if entry == "run" else review_only


def drive(case, initial, message, limit=40):
    """Run the entry point, then let the ordinary tick restart every durable wait."""
    outcomes = [initial(1, case.ctx)]
    while outcomes[-1] == "limit-wait" and len(outcomes) < limit:
        case.clock[0] = max(case.clock[0], assert_wait(case, message))
        outcomes.append(restart_at_wait_end(case))
    return outcomes
