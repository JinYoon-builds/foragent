from datetime import UTC, datetime, timedelta
import json
import os
import subprocess
import sys

import pytest

from foragent import __version__, cli
from foragent.config import Config, ConfigError
from foragent.git import GitError
from foragent.reasons import Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError


NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
BODY = "## Done when\n```sh\ntrue\n```\n"


class FakeGit:
    def __init__(self):
        self.fetches = []
        self.remotes = []

    def fetch(self, remote, ref):
        self.fetches.append((remote, ref))

    def remote_slug(self):
        self.remotes.append("origin")
        return "owner/repo"

    def blob_shas(self, ref, paths):
        return {path: "missing" for path in paths}

    def remove_worktree(self, path):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    git = FakeGit()
    tracker = FakeTracker(now=lambda: NOW, strict=True)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    (tmp_path / ".foragent.yml").write_text("approvers: [human]\n", encoding="utf-8")
    calls = {"roots": [], "repos": [], "stores": [], "launches": []}

    def git_repo(root):
        calls["roots"].append(root)
        return git

    def gh_tracker(repo):
        calls["repos"].append(repo)
        return tracker

    def for_repo(repo):
        calls["stores"].append(repo)
        return store

    def spawn(root, selected_store):
        assert root == tmp_path and selected_store is store
        return lambda n: calls["launches"].append(n) or 1234

    monkeypatch.setattr(cli, "GitRepo", git_repo)
    monkeypatch.setattr(cli, "GhTracker", gh_tracker)
    monkeypatch.setattr(cli.StateStore, "for_repo", for_repo)
    monkeypatch.setattr(cli, "spawn_runner", spawn)
    monkeypatch.setattr(cli, "load_config", lambda root: Config(approvers=["human"]))
    return tmp_path, git, tracker, store, calls


def add_approved(tracker, number=1):
    tracker.add_issue(number, f"Issue {number}", BODY)
    tracker.record_label(number, "approved", "human")


def test_tick_no_start_json_builds_context_and_promotes(app, capsys):
    root, git, tracker, store, calls = app
    add_approved(tracker)
    assert cli.main(["gate", "tick", "--repo-root", str(root), "--no-start", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"skipped": None, "actions": ["#1 approval snapshot saved", "#1 promoted to ready"]}
    assert git.fetches == [("origin", "main")]
    assert calls["roots"] == [root]
    assert calls["repos"] == calls["stores"] == ["owner/repo"]
    assert calls["launches"] == [] and store.active_runs() == []


def test_tick_starts_by_default_and_prints_plain_report(app, capsys):
    root, git, tracker, store, calls = app
    add_approved(tracker)
    assert cli.main(["gate", "tick", "--repo-root", str(root)]) == 0
    assert "#1 started running (pid=1234)" in capsys.readouterr().out
    assert calls["launches"] == [1] and store.get_run(1).pid == 1234


@pytest.mark.parametrize("json_output", [False, True])
def test_skipped_tick_succeeds(app, capsys, json_output):
    root, git, tracker, store, calls = app
    with store.tick_lock():
        args = ["gate", "tick", "--repo-root", str(root)] + (["--json"] if json_output else [])
        assert cli.main(args) == 0
    out = capsys.readouterr().out
    if json_output:
        assert json.loads(out) == {"skipped": "another tick is running", "actions": []}
    else:
        assert out == "another tick is running\n"
    assert git.fetches == [] and calls["launches"] == []


def test_config_repo_overrides_remote_slug_and_default_root_is_cwd(app, monkeypatch):
    root, git, tracker, store, calls = app
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="custom/project"))
    assert cli.main(["gate", "tick", "--no-start"]) == 0
    assert calls["roots"] == [root]
    assert calls["repos"] == ["custom/project"] and git.remotes == []


@pytest.mark.parametrize("json_output", [True, False])
def test_status_groups_latest_reason_active_runs_and_last_ten(app, capsys, json_output):
    root, git, tracker, store, calls = app
    for number, labels in (
        (5, []), (4, ["approved"]), (3, ["ready", "approved"]),
        (2, ["running", "ready", "approved"]), (1, ["needs-human", "running", "ready"]),
    ):
        tracker.add_issue(number, f"Issue {number}", BODY, labels)
    tracker.comment(1, format_comment(Reason.SPEC_QUESTION, "First question."))
    tracker._now = lambda: NOW + timedelta(minutes=1)
    tracker.comment(1, format_comment(Reason.HASH_MISMATCH, "Re-approve."))
    tracker.comment(1, "A later unrelated comment.")
    for number in range(10, 22):
        store.start_run(number, branch="old", worktree="old")
        store.finish_run(number, "not-implemented")
    store.start_run(2, branch="foragent/issue-2", worktree=store.worktree_path(2), pid=123)
    args = ["gate", "status", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        status = json.loads(output)
        assert status["issues"] == {
            "needs-human": [{"number": 1, "title": "Issue 1", "reason": "hash-mismatch"}],
            "running": [{"number": 2, "title": "Issue 2"}],
            "pr-open": [],
            "ready": [{"number": 3, "title": "Issue 3"}],
            "approved-waiting": [{"number": 4, "title": "Issue 4"}],
            "unapproved": [{"number": 5, "title": "Issue 5"}],
        }
        assert [run["number"] for run in status["active_runs"]] == [2]
        assert [run["number"] for run in status["history"]] == list(range(12, 22))
    else:
        assert "#1 Issue 1 [hash-mismatch]" in output
        assert "#2 foragent/issue-2 pid=123" in output
        assert "#10 not-implemented" not in output and "#21 not-implemented" in output
    assert git.fetches == [] and calls["launches"] == []


@pytest.mark.parametrize("json_output", [True, False])
@pytest.mark.parametrize("labels,group", [
    (["approved"], "pr-open"),
    (["approved", "ready"], "pr-open"),
    (["approved", "running"], "running"),
    (["approved", "needs-human"], "needs-human"),
])
def test_status_groups_issues_awaiting_pr_review(app, capsys, json_output, labels, group):
    root, git, tracker, store, calls = app
    tracker.add_issue(1, "Awaiting review", BODY, labels)
    tracker.create_pr(head="foragent/issue-1", base="main", title="PR", body="Closes #1")
    args = ["gate", "status", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        groups = json.loads(output)["issues"]
        assert [item["number"] for item in groups[group]] == [1]
        assert sum(len(items) for items in groups.values()) == 1
    else:
        assert f"{group}:\n  #1 Awaiting review" in output


@pytest.mark.parametrize("pid", [None, 123])
def test_run_issue_records_own_pid_and_runs_without_approval(app, monkeypatch, pid):
    root, git, tracker, store, calls = app
    add_approved(tracker)
    tracker.add_labels(1, ["running"])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=pid)
    finish_run = store.finish_run
    calls = []

    def finish(number, **kwargs):
        calls.append(number)
        assert store.get_run(number).pid == os.getpid()
        return finish_run(number, **kwargs)

    monkeypatch.setattr(store, "finish_run", finish)
    assert cli.main(["gate", "run-issue", "1", "--repo-root", str(root)]) == 0
    assert calls == [1]
    assert store.active_runs() == []
    assert store.get_run(1).outcome == "no-approval"
    assert store.get_run(1).pid == os.getpid()
    assert store.read_history() == [store.get_run(1)]
    assert tracker.get_issue(1).labels == {"approved"}
    assert tracker.get_issue(1).body == BODY and tracker.get_issue(1).state == "open"


def test_missing_run_is_one_clear_error(app, capsys):
    root, *_ = app
    assert cli.main(["gate", "run-issue", "99", "--repo-root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "foragent: no run record for issue #99\n"


@pytest.mark.parametrize("outcome,code", [("merged", 0), ("error", 1)])
def test_cli_dispatches_to_runner_and_returns_failure_code(app, monkeypatch, outcome, code):
    root, git, tracker, store, _ = app
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    calls = []

    def runner(number, ctx):
        calls.append(number)
        assert ctx.repo_root == root and ctx.git is git and ctx.tracker is tracker
        assert ctx.store is store and store.get_run(number).pid == os.getpid()
        return outcome

    monkeypatch.setattr(cli, "run_issue", runner)
    assert cli.main(["gate", "run-issue", "1", "--repo-root", str(root)]) == code
    assert calls == [1]


@pytest.mark.parametrize("error", [
    ConfigError("invalid configuration\nmore details"),
    TrackerError("gh: authentication required\nHTTP 401"),
    GitError("fatal: cannot fetch origin"),
])
def test_error_exit_code_and_single_stderr_line(app, monkeypatch, capsys, error):
    root, git, tracker, store, calls = app

    def fail(*args):
        raise error

    if isinstance(error, ConfigError):
        monkeypatch.setattr(cli, "load_config", fail)
    elif isinstance(error, TrackerError):
        monkeypatch.setattr(tracker, "list_open_issues", fail)
    else:
        monkeypatch.setattr(git, "fetch", fail)
    assert cli.main(["gate", "tick", "--repo-root", str(root), "--no-start"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err.startswith("foragent: ")


def test_module_entry_point_version():
    result = subprocess.run(
        [sys.executable, "-m", "foragent.cli", "--version"], check=True,
        capture_output=True, text=True,
    )
    assert result.stdout == f"foragent {__version__}\n" and result.stderr == ""


def test_module_entry_point_configuration_error(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "foragent.cli", "gate", "tick", "--repo-root", str(tmp_path), "--no-start"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert result.stdout == "" and "not a Foragent repo" in result.stderr
