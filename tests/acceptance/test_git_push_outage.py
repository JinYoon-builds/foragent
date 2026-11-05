"""Draft locks: a GitHub server error on `git push` waits and resumes like a gh outage.

#1644 made gh transport failures pause and resume. A rejected `git push` whose
output shows a GitHub server error (``remote: Internal Server Error`` or an
HTTP 5xx) is the same kind of fault. Use public runner/tick entry points; only
the Git remote's push answer, time and executors are offline fakes. Assertions
concern durable results: run outcomes, labels, the PR head and the remote branch.
"""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest

from conftest import git_command
from foragent.runner import review_only, run_issue
from test_github_drop import assert_merged_once, assert_wait, outage_case, restart_at_wait_end
from test_skills_autosync import isolated_home  # noqa: F401


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
HTTP_502 = (
    "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502\n"
    "send-pack: unexpected disconnect while reading sideband packet\n"
    "fatal: the remote end hung up unexpectedly\n"
)
RULESET = (
    f"remote: error: GH013: Repository rule violations found for refs/heads/{BRANCH}.\n"
    "remote: - Cannot update this protected ref.\n"
    "To https://github.com/owner/repo\n"
    f" ! [remote rejected]   HEAD -> {BRANCH} (push declined due to repository rule violations)\n"
    "error: failed to push some refs to 'https://github.com/owner/repo'\n"
)
PERMISSION = (
    "remote: Permission to owner/repo.git denied to foragent-bot.\n"
    "fatal: unable to access 'https://github.com/owner/repo/': The requested URL returned error: 403\n"
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


def history(case):
    path = case.store.root / "runs.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("number") == 1]


def evidence(case):
    """What a person reads: the run detail and the issue's comments."""
    comments = [comment.body for comment in case.tracker.comments.get(1, [])]
    return "\n".join([case.store.get_run(1).detail or "", *comments])


def held_by(case):
    return {event.label for event in case.tracker.label_events(1) if event.action == "labeled"} & {
        "blocked", "needs-human"}


@pytest.mark.parametrize("entry", ["run", "review"])
@pytest.mark.parametrize(("diagnostic", "marker"), [(INTERNAL, "Internal Server Error"), (HTTP_502, "HTTP 502")],
                         ids=["internal-server-error", "http-502"])
def test_server_error_push_waits_then_publishes_the_same_head(
    git_history, tmp_path, monkeypatch, entry, diagnostic, marker,
):
    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    initial = start(case, entry)
    remote = Remote(case, [diagnostic])
    implementations, reviews = len(case.implementer.calls), len(case.reviewer.calls)
    began = case.clock[0]

    outcomes = drive(case, initial, marker)

    assert "error" not in outcomes, f"A GitHub server error on push is not an issue failure: {outcomes}"
    assert outcomes[-1] == "merged", outcomes
    assert_merged_once(case)
    assert not held_by(case), "No person or orchestrator retry was needed"
    assert not any(row.get("outcome") == "error" for row in history(case))
    assert case.clock[0] > began, "The push was repeated after a wait, not in a hot loop"
    failed = remote.tried[0]
    assert remote.landed and remote.landed[-1] == failed, "The same verified head is pushed again"
    pr = case.tracker.created_prs[0].number
    assert case.tracker.pr_head_sha(pr) == failed == remote.head()
    # The PASS already in hand covers this head: no new implementation or review.
    assert len(case.implementer.calls) == implementations + (1 if entry == "run" else 0)
    assert len(case.reviewer.calls) == reviews + 1


@pytest.mark.parametrize("entry", ["run", "review"])
def test_server_error_that_outlasts_the_wait_ends_as_today(git_history, tmp_path, monkeypatch, entry):
    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    initial = start(case, entry)
    remote = Remote(case, [INTERNAL] * 500)
    began = case.clock[0]

    outcomes = drive(case, initial, "Internal Server Error")

    assert outcomes[-1] == "error", outcomes
    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.tracker.merged_prs == []
    assert case.store.load_flag(1) is not None
    assert "Internal Server Error" in evidence(case)
    assert len(remote.tried) >= 2, "The push is repeated after the wait before giving up"
    waited = case.clock[0] - began
    assert timedelta(0) < waited <= timedelta(minutes=BUDGET_MINUTES + 2), waited


@pytest.mark.parametrize("entry", ["run", "review"])
@pytest.mark.parametrize(("rejection", "refusal"), [(RULESET, "rule violations"), (PERMISSION, "error: 403")],
                         ids=["ruleset", "permission-403"])
def test_rejection_that_is_not_a_server_error_does_not_wait(
    git_history, tmp_path, monkeypatch, entry, rejection, refusal,
):
    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    initial = start(case, entry)
    # A server error first (waits), then GitHub answers every push with a definite refusal.
    remote = Remote(case, [INTERNAL] + [rejection] * 50)
    began = case.clock[0]

    outcomes = drive(case, initial, "Internal Server Error")

    assert outcomes[-1] == "error", outcomes
    assert len(remote.tried) >= 2 and remote.landed == []
    assert case.tracker.merged_prs == []
    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.store.load_flag(1) is not None
    assert refusal in evidence(case)
    waited = case.clock[0] - began
    # The server error waited; the refusal ended the run at once instead of
    # spending the transient budget.
    assert timedelta(0) < waited < timedelta(minutes=BUDGET_MINUTES // 2), waited


def test_branch_moved_during_the_wait_is_not_overwritten(git_history, tmp_path, monkeypatch):
    case = outage_case(git_history, tmp_path, monkeypatch, "review")
    initial = start(case, "review")
    pr = case.tracker.created_prs[0].number
    reviewed = case.tracker.pr_head_sha(pr)
    other = []

    def someone_pushes():
        if other:
            return
        stream = (f"commit refs/heads/{BRANCH}\n"
                  "committer Someone <someone@example.com> 1792483200 +0000\n"
                  "data 6\nOther\n"
                  f"from {reviewed}\n"
                  "M 100644 inline other-writer.txt\ndata 6\nother\n\ndone\n")
        git_command(case.history.origin, "fast-import", "--quiet", input=stream)
        other.append(git_command(case.history.origin, "rev-parse", f"refs/heads/{BRANCH}"))
        case.tracker.set_pr_head_sha(pr, other[0])

    remote = Remote(case, [INTERNAL], on_failure=someone_pushes)
    reviews = len(case.reviewer.calls)

    outcomes = drive(case, initial, "Internal Server Error")

    assert "error" not in outcomes, outcomes
    final = remote.head()
    assert git_command(case.history.origin, "merge-base", "--is-ancestor", other[0], final) == "", (
        "The other writer's commit must survive the repeated push")
    if outcomes[-1] == "merged":
        assert len(case.reviewer.calls) > reviews + 1, "The moved head needs its own review before merge"
    else:
        assert outcomes[-1] == "held", outcomes
        assert case.tracker.merged_prs == []
        assert case.store.load_flag(1) is not None
