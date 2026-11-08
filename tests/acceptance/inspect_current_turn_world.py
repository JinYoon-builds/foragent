"""Shared world for the n73-n76-inspect-current-turn acceptance and guard tests.

Only GitHub (FakeTracker) and the clock source are fake; state, park verbs and
the inbox run through the public `fa` entry points.
"""

from dataclasses import replace
import json

import pytest

from foragent import cli
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker


HUMAN_MERGE = "awaits a human merge decision"
PERSON_GROUPS = {"decision", "reapprove", "review", "human-check", "tech-failure"}


class World:
    def __init__(self, tmp_path, monkeypatch, **config):
        self.root = tmp_path / "repo"
        self.root.mkdir()
        self.tracker = FakeTracker()
        self.store = StateStore(tmp_path / "state")
        self.config = replace(Config(), repo="owner/repo", language="en", **config)
        monkeypatch.setattr(cli, "load_config", lambda root: self.config)
        monkeypatch.setattr(cli, "GhTracker", lambda repo: self.tracker)
        monkeypatch.setattr(cli, "GitRepo", lambda root: object())
        monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: self.store))

    def issue_with_pr(self, n, labels=("approved",)):
        self.tracker.add_issue(n, f"Issue {n}", "Body", list(labels))
        return self.tracker.create_pr(head=f"foragent/issue-{n}", base="main", title=f"PR for {n}",
                                      body=f"Closes #{n}")

    def running(self, n):
        self.store.start_run(n, branch=f"foragent/issue-{n}", worktree=self.store.worktree_path(n))
        self.tracker.add_labels(n, ["running"])

    def fa(self, capsys, *argv):
        capsys.readouterr()
        code = cli.main([*argv, "--repo-root", str(self.root)])
        out = capsys.readouterr()
        assert code == 0, f"fa {' '.join(argv)} failed: {out.out}\n{out.err}"
        return out.out

    def inspect(self, capsys):
        payload = json.loads(self.fa(capsys, "inspect", "--json"))
        lines = self.fa(capsys, "inspect").splitlines()
        return payload, lines


@pytest.fixture
def world(tmp_path, monkeypatch):
    return lambda **config: World(tmp_path, monkeypatch, **config)


def items_for(payload, n):
    return [item for item in payload["items"] if item["number"] == n]


def lines_for(lines, n):
    return [line for line in lines if f"#{n} " in line and " — " in line]


def assert_not_merge_decision(payload, lines, n, pr, why):
    shown = [(item["group"], item["summary"]) for item in payload["items"]
             if item["number"] == n or item["pr"] == pr]
    assert not [entry for entry in shown if entry[0] == "review"], (
        f"{why}: #{n} must not be review work in fa inspect --json: {shown}")
    assert not [entry for entry in shown if HUMAN_MERGE in entry[1]], (
        f"{why}: #{n} must not be described as a human merge decision: {shown}")
    for line in lines_for(lines, n):
        assert "[review]" not in line and HUMAN_MERGE not in line, f"{why}: {line}"


def assert_counts_match(payload):
    counts = payload["summary"]["counts"]
    for group in PERSON_GROUPS:
        assert counts.get(group, 0) == sum(item["group"] == group for item in payload["items"]), (
            f"The {group} count must match the items shown: {counts}")
    assert payload["summary"]["estimated_minutes"] == sum(
        counts.get(group, 0) * (3 if group == "review" else 1) for group in counts), payload["summary"]


def assert_review(payload, lines, n, pr):
    got = [(item["group"], item["pr"]) for item in items_for(payload, n)]
    assert got == [("review", pr)], f"#{n} genuinely awaits a human merge decision: {got}"
    line, = lines_for(lines, n)
    assert "[review]" in line and HUMAN_MERGE in line, line
