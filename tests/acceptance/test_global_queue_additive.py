"""Acceptance test for #1476: add to or remove from the cross-lane priority list without replacing it.

On 2026-11-01 replacing the whole list (`fa orch queue --global N...`) erased priorities
several times without anyone noticing. `--add N` and `--remove N` change one issue and keep
every other entry in its place. Every call goes through the real CLI with a fresh state
store, so the list must persist in the state directory, not in memory. The list is read
back only through the existing `--json` output.
"""

import json

import pytest

from foragent import cli
from foragent.config import Config
from foragent.state import StateStore


@pytest.fixture
def queue(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="o/r"))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: cls(tmp_path / "state")))

    def invoke(*argv):
        try:
            code = cli.main(["orch", "queue", "--global", *map(str, argv), "--repo-root", str(tmp_path)])
        except SystemExit as exc:
            code = exc.code
        out = capsys.readouterr()
        return (0 if code is None else code), out.out, out.err

    return invoke


def order(queue):
    code, out, err = queue("--json")
    assert code == 0, err
    return json.loads(out)["order"]


def ok(queue, *argv):
    code, _, err = queue(*argv)
    assert code == 0, f"fa orch queue --global {' '.join(map(str, argv))} must succeed (exit={code}):\n{err}"


def test_add_appends_one_issue_and_keeps_every_other_entry_in_place(queue):
    ok(queue, "--add", 7)  # An empty list is created by the first add.
    assert order(queue) == [7]
    ok(queue, 85, 45)
    ok(queue, "--add", 12)
    assert order(queue) == [85, 45, 12]
    ok(queue, "--add", 30)
    assert order(queue) == [85, 45, 12, 30]


def test_adding_an_issue_already_listed_keeps_its_place_and_adds_no_copy(queue):
    ok(queue, 85, 45, 12)
    ok(queue, "--add", 85)
    ok(queue, "--add", 85)
    assert order(queue) == [85, 45, 12]


def test_remove_takes_out_only_that_issue(queue):
    ok(queue, 85, 45, 12)
    ok(queue, "--remove", 45)
    assert order(queue) == [85, 12]
    ok(queue, "--remove", 85)
    ok(queue, "--remove", 12)
    assert order(queue) == []


def test_removing_an_issue_not_listed_succeeds_and_changes_nothing(queue):
    ok(queue, 85, 45)
    ok(queue, "--remove", 999)
    ok(queue, "--remove", 999)
    assert order(queue) == [85, 45]
