"""Acceptance test for stale-files. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess

import pytest

from conftest import BASE_TIME, git_command
from foragent import executors, scheduler
from foragent.state import StateStore
from test_lane_width_by_overlap import _wide_lane
from test_scheduler import BODY, approve, ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


CLI = "src/foragent/cli.py"
COMMAND = "src/foragent/commands/run.py"


@pytest.fixture(autouse=True)
def offline_stale_files(tmp_path, monkeypatch, isolated_home, offline_acceptance):
    """Permit only fixture-local Git subprocesses; never launch agents or signal."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Stale-files tests must not signal processes or launch agents")

    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)


@pytest.fixture
def stale_context(ctx):
    # A non-pytest command keeps unrelated baseline builds out of these ticks.
    ctx.config = replace(ctx.config, skill_libraries=[], test_command="true")
    return ctx


def _body(*files):
    return BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files)


def _tick(context):
    report = scheduler.tick(context)
    assert report.skipped is None, report
    return report


def _later(context):
    now = context.now() + timedelta(minutes=5)
    context.now = lambda: now


def _events(context):
    path = context.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    return [event for line in path.read_text(encoding="utf-8").splitlines()
            if (event := json.loads(line))["kind"] == "stale-files"]


def _unchanged_approval(context, number, approval, labels):
    assert context.store.load_approval(number) == approval
    assert context.tracker.get_issue(number).labels == set(labels)
    assert context.store.load_flag(number) is None


def test_stale_files_deleted_path_is_reported_once_per_main_sha(stale_context, git_history):
    context = stale_context
    git_history.update({"src/foragent/__init__.py": "", CLI: "# Old CLI\n"},
                       at=BASE_TIME + timedelta(minutes=1))
    approve(context, 334, body=_body(CLI))
    _tick(context)
    approval = context.store.load_approval(334)
    assert approval is not None
    assert _events(context) == []

    # A deleted, previously existing path is distinct from a new file proposal,
    # even though src/foragent still exists. Only origin/main is authoritative.
    local = context.repo_root / CLI
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("# A leftover local copy must not hide the deletion\n")
    git_history.update({CLI: None, COMMAND: "# Replacement command\n"},
                       at=context.now() + timedelta(minutes=1))
    _later(context)
    _tick(context)
    first_sha = git_command(context.repo_root, "rev-parse", "origin/main")
    first = _events(context)
    assert first_sha == git_history.tip
    assert git_command(context.repo_root, "ls-tree", "-r", "--name-only", "origin/main", CLI) == ""
    assert local.is_file()
    _unchanged_approval(context, 334, approval, {"approved", "ready"})

    # Deduplication must survive reopening the persisted state store.
    context.store = StateStore(context.store.root, now=context.now)
    _later(context)
    _tick(context)
    same_sha = _events(context)
    assert git_command(context.repo_root, "rev-parse", "origin/main") == first_sha

    git_history.update({"README.md": "An unrelated main change\n"},
                       at=context.now() + timedelta(minutes=1))
    _later(context)
    _tick(context)
    next_sha = git_command(context.repo_root, "rev-parse", "origin/main")
    changed_sha = _events(context)
    assert next_sha == git_history.tip and next_sha != first_sha
    _unchanged_approval(context, 334, approval, {"approved", "ready"})
    assert context.store.active_runs() == []

    assert (len(first), len(same_sha), len(changed_sha)) == (1, 1, 2), (
        "A stale Files entry must emit once per issue and origin/main SHA",
        first, same_sha, changed_sha,
    )
    assert same_sha == first and changed_sha[:1] == first
    assert all(event["issue"] == 334 and CLI in event["summary"] for event in changed_sha)


def test_stale_files_new_path_and_matching_glob_are_not_reported(stale_context, git_history):
    context = stale_context
    new_file = "src/foragent/new_module.py"
    matching_glob = "src/foragent/commands/*.py"
    retired = "src/foragent/retired/control.py"
    git_history.update({COMMAND: "# Existing command\n", retired: "# Retired package\n"},
                       at=BASE_TIME + timedelta(minutes=1))
    approve(context, 1, body=_body(new_file, matching_glob))
    # A separate stale issue proves the scan actually ran: a blanket no-op must
    # not pass the negative assertions about the two valid Files entries.
    approve(context, 2, body=_body(retired))
    _tick(context)
    approvals = {number: context.store.load_approval(number) for number in (1, 2)}
    assert all(approval is not None for approval in approvals.values())
    assert _events(context) == []

    git_history.update({retired: None}, at=context.now() + timedelta(minutes=1))
    _later(context)
    _tick(context)

    assert git_command(context.repo_root, "cat-file", "-t", "origin/main:src/foragent") == "tree"
    assert git_command(context.repo_root, "ls-tree", "-r", "--name-only", "origin/main", new_file) == ""
    assert git_command(context.repo_root, "ls-tree", "-r", "--name-only", "origin/main",
                       "src/foragent/commands") == COMMAND
    assert not (context.repo_root / new_file).exists()
    assert not (context.repo_root / COMMAND).exists()
    for number, approval in approvals.items():
        _unchanged_approval(context, number, approval, {"approved", "ready"})
    events = _events(context)
    assert not any(event["issue"] == 1 for event in events), events
    assert len(events) == 1 and events[0]["issue"] == 2, (
        "Report the stale control issue while allowing new files and matching globs", events,
    )
    assert retired in events[0]["summary"]
    assert new_file not in events[0]["summary"] and matching_glob not in events[0]["summary"]


def test_stale_files_gone_glob_prefix_is_a_fact_and_does_not_change_starts(stale_context, git_history):
    context = stale_context
    old_file = "src/foragent/old_pkg/module.py"
    stale_glob = "src/foragent/old_pkg/**"
    git_history.update({old_file: "# Old package\n", COMMAND: "# Existing command\n"},
                       at=BASE_TIME + timedelta(minutes=1))
    approve(context, 1, body=_body(stale_glob), labels=["lane:orch"])
    approve(context, 2, body=_body(COMMAND), labels=["lane:orch"])
    _tick(context)
    approvals = {number: context.store.load_approval(number) for number in (1, 2)}
    assert all(approval is not None for approval in approvals.values())
    assert _events(context) == []

    git_history.update({old_file: None}, at=context.now() + timedelta(minutes=1))
    local = context.repo_root / old_file
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("# The glob still matches locally, but not on origin/main\n")
    starts = _wide_lane(context, 2)
    _later(context)
    report = _tick(context)

    assert git_command(context.repo_root, "ls-tree", "-r", "--name-only", "origin/main",
                       "src/foragent/old_pkg") == ""
    assert local.is_file()
    assert starts == [1, 2], report.actions
    assert {run.number for run in context.store.active_runs()} == {1, 2}
    for number, approval in approvals.items():
        _unchanged_approval(context, number, approval, {"approved", "running", "lane:orch"})
    events = _events(context)
    assert len(events) == 1 and events[0]["issue"] == 1, (
        "A gone fixed glob prefix must be reported without holding or reordering starts", events,
    )
    assert stale_glob in events[0]["summary"]
    assert COMMAND not in events[0]["summary"]
