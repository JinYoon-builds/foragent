"""Acceptance (neutral-during-train, statement 5). Locked by Foragent; do not edit.

When a train's tip CI run is cancelled or hits its job time limit with no failed
test, the owner re-runs that CI and does not retire the train.

PM r990: on 2026-11-07 the owner retired trains #2432 and #2440 because GitHub
cancelled acceptance shard 0 at its 30-minute job limit, with no failed test.
Today `gh pr checks` reports that shard in the `cancel` bucket (or as TIMED_OUT
in the `fail` bucket), the tracker answers "fail", and the owner retires the
train as "tip CI red".

Real gate tick, merge owner, merge train, Git and state of the locked
merge-path world (tests/test_merge_owner_batch.py `batched`). The train's CI is
answered by the real GhTracker over a fake `gh` (the GitHub Actions REST API and
`gh pr checks`): every tracker method about checks, workflow runs or re-runs is
served by GhTracker for the train's PR, head or branch. A re-run is a POST to
`actions/runs/<id>/rerun` or `actions/runs/<id>/rerun-failed-jobs`, or
`gh run rerun <id>`. No real process or network is used.
"""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import batched, events, green, groups, queue_workers, refs  # noqa: E402, F401
from foragent.ci_workflow import WORKFLOW  # noqa: E402
from foragent.tracker import GhTracker  # noqa: E402

MEMBERS = [5, 1]
LATER = [4, 2]
RERUN = re.compile(r"actions/runs/(\d+)/(rerun|rerun-failed-jobs)$")


class FakeActions:
    """GitHub Actions for train tips; one workflow run per head, shard jobs inside it."""

    def __init__(self, case):
        self.case = case
        self.phase = {}       # head -> pending | cancelled | timed_out | failure | rerunning | pass
        self.run_ids = {}     # head -> run id
        self.reruns = []      # run ids a re-run was requested for
        self.cancels = []

    # ---- train identity
    def trains(self):
        rows = []
        for pr in groups(self.case):
            rows.append((pr.number, pr.head, self.case.tracker.pr_head_sha(pr.number)))
        return rows

    def owns(self, value):
        return any(value in (number, branch, head) for number, branch, head in self.trains())

    def head_of(self, value):
        return next(head for number, branch, head in self.trains() if value in (number, branch, head))

    def run_id(self, head):
        return self.run_ids.setdefault(head, 9000 + len(self.run_ids))

    def head_for_run(self, run_id):
        return next(head for head, rid in self.run_ids.items() if rid == run_id)

    # ---- shapes
    def shard_states(self, head):
        phase = self.phase.get(head, "pending")
        shards = self.case.config.ci_shards
        first = {"pending": ("IN_PROGRESS", "pending"), "rerunning": ("IN_PROGRESS", "pending"),
                 "cancelled": ("CANCELLED", "cancel"), "timed_out": ("TIMED_OUT", "fail"),
                 "failure": ("FAILURE", "fail"), "pass": ("SUCCESS", "pass")}[phase]
        rest = ("IN_PROGRESS", "pending") if phase in {"pending", "rerunning"} else ("SUCCESS", "pass")
        return [first] + [rest] * (shards - 1)

    def checks(self, head):
        items = [{"bucket": bucket, "state": state, "name": f"acceptance (shard {i})",
                  "workflow": "foragent-acceptance", "link": f"https://github.test/run/{self.run_id(head)}",
                  "startedAt": "2026-11-07T01:00:00Z"}
                 for i, (state, bucket) in enumerate(self.shard_states(head))]
        return items

    def run(self, head):
        phase = self.phase.get(head, "pending")
        branch = next(b for _, b, h in self.trains() if h == head)
        done = phase not in {"pending", "rerunning"}
        conclusion = {"cancelled": "cancelled", "timed_out": "timed_out", "failure": "failure",
                      "pass": "success"}.get(phase)
        repo = {"full_name": self.case.ctx.repo}
        return {"id": self.run_id(head), "head_sha": head, "head_branch": branch, "event": "pull_request",
                "path": WORKFLOW, "name": "foragent-acceptance", "status": "completed" if done else "in_progress",
                "conclusion": conclusion, "run_attempt": 1 + self.reruns.count(self.run_id(head)),
                "repository": repo, "head_repository": repo, "html_url": f"https://github.test/run/{self.run_id(head)}"}

    def jobs(self, head):
        rows = []
        for i, (state, _bucket) in enumerate(self.shard_states(head)):
            done = state not in {"IN_PROGRESS"}
            rows.append({"id": 100 * self.run_id(head) + i, "run_id": self.run_id(head),
                         "name": f"acceptance (shard {i})", "status": "completed" if done else "in_progress",
                         "conclusion": state.lower() if done else None,
                         "started_at": "2026-11-07T01:00:00Z",
                         "completed_at": "2026-11-07T01:30:00Z" if done else None})
        return rows

    # ---- the fake gh
    def __call__(self, args, input=None):
        args = [str(a) for a in args]
        if args[:2] == ["pr", "checks"]:
            head = self.head_of(int(args[2]))
            items = self.checks(head)
            buckets = {item["bucket"] for item in items}
            out = json.dumps(items)
            if buckets & {"fail", "cancel"}:
                raise subprocess.CalledProcessError(1, ["gh", *args], output=out, stderr="")
            if "pending" in buckets:
                raise subprocess.CalledProcessError(8, ["gh", *args], output=out, stderr="")
            return out
        if args[:2] == ["run", "rerun"]:
            self._rerun(int(args[2]))
            return ""
        if args[:1] == ["api"]:
            path = args[-1]
            if "-X" in args and args[args.index("-X") + 1] == "POST":
                if match := RERUN.search(path):
                    self._rerun(int(match.group(1)))
                    return ""
                if path.endswith("/cancel"):
                    self.cancels.append(int(path.split("/")[-2]))
                    return ""
            if "/actions/runs?" in path:
                sha = re.search(r"head_sha=([0-9a-f]+)", path)
                head = sha.group(1) if sha else None
                runs = [self.run(head)] if head and self.owns(head) else []
                return json.dumps(runs if ".workflow_runs" in args else {"total_count": len(runs),
                                                                            "workflow_runs": runs})
            if match := re.search(r"actions/runs/(\d+)/jobs", path):
                jobs = self.jobs(self.head_for_run(int(match.group(1))))
                return json.dumps(jobs if ".jobs" in args else {"total_count": len(jobs), "jobs": jobs})
            if match := re.search(r"actions/runs/(\d+)$", path):
                return json.dumps(self.run(self.head_for_run(int(match.group(1)))))
        raise AssertionError(f"unexpected gh call in the fake Actions API: {args}")

    def _rerun(self, run_id):
        head = self.head_for_run(run_id)
        assert self.phase.get(head) in {"cancelled", "timed_out"}, (
            f"Only a cancelled or timed-out tip CI run is re-run, not a {self.phase.get(head)} one")
        self.reruns.append(run_id)
        self.phase[head] = "rerunning"


def serve_train_ci(case, monkeypatch):
    """Route the tracker's check, workflow-run and re-run methods to GhTracker for train refs."""
    actions = FakeActions(case)
    gh = GhTracker(case.ctx.repo, run=actions)
    names = [name for name in dir(GhTracker)
             if not name.startswith("_") and callable(getattr(GhTracker, name))
             and any(word in name for word in ("check", "workflow", "rerun"))
             and name not in {"pr_checks_started"}]
    for name in names:
        original = getattr(case.tracker, name, None)
        served = getattr(gh, name)

        def routed(*args, _original=original, _served=served, **kwargs):
            first = args[0] if args else next(iter(kwargs.values()), None)
            if _original is None or (first is not None and actions.owns(first)):
                return _served(*args, **kwargs)
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.tracker, name, routed, raising=False)
    return actions


def members_landed(case):
    return {n for n, _ in case.store.owner_merge_facts()}


def retired_reasons(case):
    return [row.get("reason") for row in events(case) if row.get("reason") != "train merged"]


def waiting_train(case, members=MEMBERS):
    case.pending_batch = True
    queue_workers(case, members)
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "the train was not built"
    assert not members_landed(case), "the train must still wait for its CI"
    return groups(case)[0]


@pytest.mark.parametrize("stopped", ["cancelled", "timed_out"])
def test_cancelled_or_timed_out_tip_ci_is_re_run_and_the_train_is_kept(batched, monkeypatch, stopped):
    case = batched
    actions = serve_train_ci(case, monkeypatch)
    pr = waiting_train(case)
    tip = case.tracker.pr_head_sha(pr.number)
    run_id = actions.run_id(tip)

    # GitHub stops acceptance shard 0 at its job limit; no test failed.
    actions.phase[tip] = stopped
    support.gate(case)

    assert actions.reruns == [run_id], (
        f"A {stopped} tip CI run with no failed test is re-run by the owner, once; re-runs: {actions.reruns}")
    assert not retired_reasons(case), f"The train is not retired for a {stopped} run: {retired_reasons(case)}"
    assert refs(case) and len(groups(case)) == 1, "The train's published refs and PR stay"
    assert case.tracker.get_pr(pr.number).state == "open", "The train PR stays open"
    assert case.tracker.pr_head_sha(pr.number) == tip, "The re-run measures the same tip"
    assert not members_landed(case), "The train does not land without a finished CI"

    support.gate(case)  # The re-run is still in progress.
    assert actions.reruns == [run_id], "The owner does not re-run a CI run that is already running again"
    assert not retired_reasons(case)

    actions.phase[tip] = "pass"
    green(case)
    support.gate(case)
    support.gate(case)

    assert members_landed(case) == set(MEMBERS), "The kept train lands once its re-run CI passes"
    assert len(groups(case)) == 1, "The train is not rebuilt"
    assert actions.reruns == [run_id]
    for n in MEMBERS:
        assert git_command(case.history.origin, "show", f"main:src/member_{n}.py") == f"VALUE = {n}"

    # A failed test is still red: the next train is retired and not re-run.
    case.pending_batch = True
    queue_workers(case, LATER)
    for _ in range(3):
        support.gate(case)
        if len(groups(case)) > 1:
            break
    later = [p for p in groups(case) if p.number != pr.number]
    assert later, "A next train is built"
    later_tip = case.tracker.pr_head_sha(later[0].number)
    actions.phase[later_tip] = "failure"
    support.gate(case)
    assert "tip CI red" in retired_reasons(case), "A tip CI run with a failed test still retires the train"
    assert actions.reruns == [run_id], "A failed run is not re-run"
    assert not members_landed(case) & set(LATER)
