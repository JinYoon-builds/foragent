"""Acceptance (train-ci-first, statement 1).

When the merge owner builds a train that includes a PR whose own CI run is
pending or queued, it requests cancellation of that PR CI run.

Jin 11-08 (r1227, A): on 11-08 train CI waited behind lane PR CI. The train CI
tests the same code, so the member's own pending PR CI only takes runners.

Real gate tick, merge owner, merge train, Git and state of the merge-path world
(tests/helpers/merge_batch.py `batched`). Only GitHub is fake: the members' own
PR CI runs live in a fake GitHub Actions REST API behind the real GhTracker.
Every tracker method whose name speaks of cancelling or stopping is served by
GhTracker when its first argument names a member PR (number, branch or head);
everything else stays on the FakeTracker. A cancellation request is a POST to
`actions/runs/<id>/cancel` or `gh run cancel <id>` for one of the member's
runs. No real process or network is used.
"""

import json
from pathlib import Path
import re
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
from helpers.merge_world import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from helpers.merge_batch import batched, green, groups, queue_workers, refs  # noqa: E402, F401
from foragent import merge_owner_batch as batch  # noqa: E402
from foragent.ci_workflow import WORKFLOW  # noqa: E402
from foragent.tracker import GhTracker  # noqa: E402

MEMBERS = [5, 1]
CANCEL = re.compile(r"actions/runs/(\d+)/cancel$")


class MemberActions:
    """The members' own PR CI runs: two unfinished workflow runs and one finished run per member head."""

    def __init__(self, case, status):
        self.case = case
        self.status = status       # GitHub run status of the unfinished runs: queued | in_progress
        self.runs = {}             # run id -> run dict
        self.cancels = []          # run ids a cancellation was requested for
        self.calls = []

    def member(self, n):
        pr = support.pr_for(self.case, n)
        return pr.number, pr.head, self.case.tracker.pr_head_sha(pr.number)

    def members(self):
        return [self.member(n) for n in MEMBERS if any(
            pr.head == f"foragent/issue-{n}" for pr in self.case.tracker.created_prs)]

    def owns(self, value):
        return any(value in row for row in self.members())

    def seed(self):
        repo = {"full_name": self.case.ctx.repo}
        for number, branch, head in self.members():
            for k, (name, path, status) in enumerate([
                    ("ci", ".github/workflows/ci.yml", self.status),
                    ("foragent-acceptance", WORKFLOW, self.status),
                    ("clean-install", ".github/workflows/clean-install.yml", "completed")]):
                run_id = 7000 + 10 * number + k
                self.runs[run_id] = {
                    "id": run_id, "head_sha": head, "head_branch": branch, "event": "pull_request",
                    "path": path, "name": name, "status": status,
                    "conclusion": "success" if status == "completed" else None, "run_attempt": 1,
                    "repository": repo, "head_repository": repo,
                    "pull_requests": [{"number": number, "head": {"ref": branch, "sha": head}}],
                    "html_url": f"https://github.test/run/{run_id}"}

    def unfinished(self, head):
        return sorted(run_id for run_id, run in self.runs.items()
                      if run["head_sha"] == head and run["status"] != "completed")

    def finished(self, head):
        return sorted(run_id for run_id, run in self.runs.items()
                      if run["head_sha"] == head and run["status"] == "completed")

    def _cancel(self, run_id):
        self.cancels.append(run_id)
        run = self.runs.get(run_id)
        if run is not None and run["status"] != "completed":
            run["status"], run["conclusion"] = "completed", "cancelled"

    def _listed(self, query):
        rows = list(self.runs.values())
        for key in ("head_sha", "branch", "status", "event"):
            if match := re.search(rf"[?&]{key}=([^&]+)", query):
                value = match.group(1)
                field = {"branch": "head_branch"}.get(key, key)
                rows = [run for run in rows if str(run.get(field)) == value]
        return rows

    def __call__(self, args, input=None):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[:2] == ["run", "cancel"]:
            self._cancel(int(args[2]))
            return ""
        if args[:2] == ["run", "list"]:
            rows = [{"databaseId": run["id"], "status": run["status"], "conclusion": run["conclusion"] or "",
                     "headSha": run["head_sha"], "headBranch": run["head_branch"], "event": run["event"],
                     "workflowName": run["name"], "name": run["name"], "url": run["html_url"]}
                    for run in self.runs.values()]
            if "--branch" in args:
                rows = [row for row in rows if row["headBranch"] == args[args.index("--branch") + 1]]
            if "--commit" in args:
                rows = [row for row in rows if row["headSha"] == args[args.index("--commit") + 1]]
            return json.dumps(rows)
        if args[:1] == ["api"]:
            path = args[-1]
            if "-X" in args and args[args.index("-X") + 1] == "POST":
                if match := CANCEL.search(path):
                    self._cancel(int(match.group(1)))
                    return ""
            if match := re.search(r"/pulls/(\d+)$", path):
                number = int(match.group(1))
                _, branch, head = next(row for row in self.members() if row[0] == number)
                return json.dumps({"number": number, "state": "open",
                                   "head": {"ref": branch, "sha": head,
                                            "repo": {"full_name": self.case.ctx.repo}},
                                   "base": {"ref": "main"}})
            if "/actions/runs?" in path or path.endswith("/actions/runs"):
                rows = self._listed(path.split("/actions/runs", 1)[1])
                return json.dumps(rows if ".workflow_runs" in args else {"total_count": len(rows),
                                                                            "workflow_runs": rows})
            if match := re.search(r"/pulls/(\d+)/commits", path) or re.search(r"actions/runs/(\d+)/jobs", path):
                return json.dumps([])
            if match := re.search(r"actions/runs/(\d+)$", path):
                return json.dumps(self.runs[int(match.group(1))])
        raise AssertionError(f"unexpected gh call in the fake Actions API for a member PR: {args}")


def serve_member_ci(case, monkeypatch, status):
    actions = MemberActions(case, status)
    gh = GhTracker(case.ctx.repo, run=actions)
    names = [name for name in dir(GhTracker)
             if not name.startswith("_") and callable(getattr(GhTracker, name))
             and any(word in name for word in ("cancel", "stop"))
             and name not in {"pr_checks_cancelled_only", "rerun_cancelled"}]
    for name in names:
        original = getattr(case.tracker, name, None)
        served = getattr(gh, name)

        def routed(*args, _original=original, _served=served, **kwargs):
            values = list(args) + list(kwargs.values())
            if _original is None or any(actions.owns(value) for value in values):
                return _served(*args, **kwargs)
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, routed, raising=False)
    return actions


def requested(actions, head):
    """Cancellation requests for this member's own runs."""
    return [run_id for run_id in actions.cancels if actions.runs.get(run_id, {}).get("head_sha") == head]


def train_tip_cancels(case):
    return [row for row in case.tracker.cancelled_workflow_runs if row[1].startswith(batch.PREFIX)]


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_building_a_train_cancels_each_members_pending_pr_ci_once(batched, monkeypatch, status):
    case = batched
    case.pending_batch = True
    actions = serve_member_ci(case, monkeypatch, status)
    queue_workers(case, MEMBERS)  # Each member's own PR CI is pending (checks) with unfinished runs.
    actions.seed()
    members = actions.members()
    assert len(members) == len(MEMBERS)
    expected = {head: actions.unfinished(head) for _, _, head in members}
    keep = {head: actions.finished(head) for _, _, head in members}
    assert all(expected.values())

    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "the train was not built"

    for number, branch, head in members:
        posted = requested(actions, head)
        assert sorted(posted) == expected[head], (
            f"Building the train must request cancellation of member PR #{number}'s own {status} CI runs "
            f"{expected[head]} (head {head[:12]}, branch {branch}), once each; requested: {posted}; "
            f"tracker cancels: {case.tracker.cancelled_workflow_runs}")
        assert not set(posted) & set(keep[head]), "A finished run is not cancelled"
    assert not train_tip_cancels(case), (
        f"The train's own CI is never cancelled while the train waits for it: {train_tip_cancels(case)}")

    # The next tick still waits for the train's CI; nothing is requested twice.
    support.gate(case)
    for number, branch, head in members:
        posted = requested(actions, head)
        assert sorted(posted) == expected[head], (
            f"Member PR #{number}'s CI cancellation is requested once, not again each tick: {posted}")
    assert not train_tip_cancels(case)

    # Train results are unchanged: the train lands on its own green CI.
    green(case)
    for _ in range(3):
        support.gate(case)
    landed = {n for n, _ in case.store.owner_merge_facts()}
    assert landed == set(MEMBERS), f"The train lands its members once its CI passes: {landed}"
    for n in MEMBERS:
        assert git_command(case.history.origin, "show", f"main:src/member_{n}.py") == f"VALUE = {n}"
