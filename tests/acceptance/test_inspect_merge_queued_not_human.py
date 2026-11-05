"""Acceptance test for inspect-merge-queued-not-human. Locked by Foragent; do not edit.

With `merge_owner: true` a worker hands its verified PR to the merge owner and
finishes as merge-queued: the PR waits for the owner's turn, not for a person
(#1836). On 2026-11-05 `fa inspect` still listed such PRs (#1422, #2147: CI
green, labels `approved` only, no question asked) as "[review] ... PR awaits a
human merge decision", so the person could merge by hand or answer a question
nobody asked.

Now a merge-queued PR is not a person's merge decision in `fa inspect`: it is
left out of the person's items, or shown as waiting for the merge owner's
turn, in the dashboard's word for that state ("turn"). PRs that really wait for
a person stay: a risk:high PR the orchestrator escalated, a careful-review PR
the gate left open for a person, and every open Foragent PR when `merge_owner`
is off.

Real runner, gate tick, merge owner, Git and state (the locked merge-path
world); only GitHub, models and the clock are fake. The person's view is the
public `fa inspect` command.
"""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from foragent import cli, merge_owner, runner
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from acceptance import test_merge_path_real_settings as contract
from acceptance.test_merge_path_real_settings import world  # noqa: F401

QUEUED = 5      # Ordinary approved work: the worker hands its PR to the merge owner.
RISKY = 1       # risk:high work: the gate holds its PR for a person.
CAREFUL = 4     # A careful-review path: the gate leaves its PR open for a person's merge.
PERSON_GROUPS = {"decision", "reapprove", "review", "human-check", "tech-failure"}
HUMAN_MERGE = "awaits a human merge decision"


@pytest.fixture
def owner_world(world, monkeypatch):
    """The locked merge-path world with `merge_owner: true`; the owner never takes a turn here."""
    case = world()
    case.config = replace(case.config, merge_owner=True)
    case.ctx.config = case.config
    case.store.finish_run(contract.BLOCKER, outcome="merged")
    for n in contract.NUMBERS:
        case.store.finish_run(n, outcome="fixture-unused")
        case.tracker.remove_labels(n, ["running"])
    case.ctx.sleep = lambda seconds: pytest.fail(f"Worker kept polling: {seconds}")
    # Only the owner's process transport is absent: its queued PRs stay unmerged.
    monkeypatch.setattr(merge_owner, "start", lambda ctx, report: None)
    return case


def gate(case):
    case.clock[0] += timedelta(seconds=1)
    tick(TickContext("owner/repo", case.root, case.tracker, case.store, case.config, case.git,
                     now=lambda: case.clock[0], launcher=lambda n: None, start_runs=False,
                     baseline_launcher=lambda *args: None))


def pr_for(case, n):
    return next(pr for pr in reversed(case.tracker.created_prs) if pr.head == f"foragent/issue-{n}")


def bind(case, monkeypatch, config):
    monkeypatch.setattr(cli, "load_config", lambda root: config[0])
    monkeypatch.setattr(cli, "GhTracker", lambda repo: case.tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda root: case.git)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: case.store))


def run_one(case, n):
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(n, branch=f"foragent/issue-{n}", worktree=case.store.worktree_path(n))
    case.tracker.add_labels(n, ["running"])
    case.worker.number = n
    return runner.run_issue(n, case.ctx)


def inspect(case, capsys):
    """(JSON payload, text lines) of the person's `fa inspect`."""
    capsys.readouterr()
    assert cli.main(["inspect", "--repo-root", str(case.root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert cli.main(["inspect", "--repo-root", str(case.root)]) == 0
    return payload, capsys.readouterr().out.splitlines()


def lines_for(lines, n):
    return [line for line in lines if f"#{n} " in line and " — " in line]


def test_merge_queued_pr_is_not_a_human_merge_decision_but_human_merges_stay(owner_world, monkeypatch, capsys):
    case = owner_world
    config = [case.config]
    bind(case, monkeypatch, config)

    # A risk:high PR: the gate holds it and the orchestrator hands it to the person.
    case.tracker.add_labels(RISKY, ["risk:high"])
    assert run_one(case, RISKY) != "merge-queued", "Setup: a risk:high PR never enters the owner's queue"
    capsys.readouterr()
    assert cli.main(["orch", "escalate", str(RISKY), "--repo-root", str(case.root),
                     "--summary", "risk:high PR needs a person's merge decision",
                     "--tried", "Read the diff; the change touches a high-risk area."]) == 0
    gate(case)
    assert "needs-human" in case.tracker.get_issue(RISKY).labels, "Setup: the risky PR waits for a person"

    # A careful-review path under merge_owner: the PR stays open for a person to merge.
    case.config = replace(case.config, review_paths={f"src/member_{CAREFUL}.py": "careful"})
    case.ctx.config = config[0] = case.config
    assert run_one(case, CAREFUL) != "merge-queued", "Setup: a careful-review PR never enters the owner's queue"
    careful_pr = pr_for(case, CAREFUL).number

    # Ordinary work: CI green, labels `approved` only, handed to the merge owner.
    assert run_one(case, QUEUED) == "merge-queued", case.store.log_path(QUEUED).read_text()
    assert case.tracker.get_issue(QUEUED).labels == {"approved"}, "Setup: no blocked/needs-human label"
    queued_pr = pr_for(case, QUEUED).number
    assert not case.tracker.merged_prs, "Setup: the owner has not merged it yet"

    payload, lines = inspect(case, capsys)
    queued = [item for item in payload["items"] if item["number"] == QUEUED or item["pr"] == queued_pr]
    assert len(queued) <= 1, f"The queued PR appears at most once: {queued}"
    assert not [item for item in queued if item["group"] in PERSON_GROUPS], (
        "Under merge_owner a merge-queued PR waits for the owner's turn, not for a person's "
        f"merge decision: {[(item['group'], item['summary']) for item in queued]}"
    )
    counts = payload["summary"]["counts"]
    for group in PERSON_GROUPS:
        assert counts.get(group, 0) == sum(item["group"] == group for item in payload["items"]), (
            f"The person's {group} count must match the items shown: {counts}"
        )
    for line in lines_for(lines, QUEUED):
        assert HUMAN_MERGE not in line and "[review]" not in line, line
        assert "turn" in line.casefold(), (
            f"A shown queued PR uses the dashboard's word for the owner's turn: {line}"
        )

    risky = [item for item in payload["items"] if item["number"] == RISKY]
    assert [item for item in risky if item["group"] in PERSON_GROUPS], (
        "A risk:high PR the orchestrator handed to the person still waits for the person under merge_owner"
    )
    assert lines_for(lines, RISKY), "The risky PR keeps its line in the person's inbox"
    careful = [(item["group"], item["pr"]) for item in payload["items"] if item["number"] == CAREFUL]
    assert careful == [("review", careful_pr)], (
        f"An open PR the owner does not hold still awaits a human merge decision under merge_owner: {careful}"
    )
    line, = lines_for(lines, CAREFUL)
    assert HUMAN_MERGE in line, line

    # Same records with merge_owner off: nobody merges for the person, so the PR is theirs.
    config[0] = replace(case.config, merge_owner=False, merge_batch=False)
    payload, lines = inspect(case, capsys)
    queued = [item for item in payload["items"] if item["number"] == QUEUED]
    assert [(item["group"], item["pr"]) for item in queued] == [("review", queued_pr)], (
        f"With merge_owner off an open Foragent PR still awaits a human merge decision: {queued}"
    )
    line, = lines_for(lines, QUEUED)
    assert HUMAN_MERGE in line, line
    assert [item for item in payload["items"] if item["number"] == RISKY], "The risky PR stays"
