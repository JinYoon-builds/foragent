"""Locked one-entrance contract. Test support below uses only offline adapters.

Session identity is the existing FORAGENT_SESSION_ID and the statusline hook's
session_id. No private PM record, helper, or implementation name is prescribed.
"""

from datetime import UTC, datetime
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from foragent import cli, executors, order
from foragent.executors import ExecResult
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_order import PublishingGit
from test_skills_autosync import isolated_home  # noqa: F401


def make_pm_case(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git/config").write_text('[remote "origin"]\nurl = git@github.com:offline/one-entry.git\n')
    values = {"repo": "offline/one-entry", "language": "en", "approvers": ["human"],
              "checks": "local", "skill_libraries": [],
              "lanes": {"api": {"paths": ["api/**"], "agent": "fa-api"},
                        "view": {"paths": ["view/**"], "agent": "fa-view"}}}
    (root / ".foragent.yml").write_text(yaml.safe_dump(values))
    tracker = FakeTracker(actor="human")
    events, calls = [], []
    git = PublishingGit(root, events)
    git.remote_slug = lambda: values["repo"]
    # Committed configuration is an external Git read, not a gate behaviour fake.
    git.blob_sha = lambda ref, path: path if (root / path).is_file() else None
    git._call = lambda args: (root / args[2]).read_text() if args[:2] == ["cat-file", "blob"] else ""
    git.fetch = lambda *args: None
    git.remove_worktree = lambda path: None
    store = StateStore.for_repo(values["repo"])
    monkeypatch.chdir(root)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda path: git)
    monkeypatch.setattr(cli.star, "offer", lambda language: None)
    monkeypatch.setattr(cli, "kick_tick", lambda *args: None)
    monkeypatch.setattr(order, "kick_tick", lambda *args: None)
    worker = SimpleNamespace(replies=["NONE"] * 20)

    def execute(**kwargs):
        calls.append(kwargs)
        assert worker.replies, "Unexpected extra model invocation"
        return ExecResult(0, False, worker.replies.pop(0), kwargs["log_path"])

    worker.run = execute
    monkeypatch.setattr(executors, "executor_for", lambda name: worker)
    monkeypatch.setattr(order, "executor_for", lambda name: worker)
    plan = {"id": "2026-10-31-pm-fixture", "title": "Offline work", "summary": "One task",
            "spec_markdown": "# Offline work\n", "issues": [{"key": "work", "title": "Keep locale",
            "goal": "Keep the selected locale", "out_of_scope": "Other locales", "lane": "api", "size": "s",
            "done_when": ["uv run pytest -q tests/acceptance/test_pm_fixture.py"],
            "acceptance_files": {"tests/acceptance/test_pm_fixture.py": "def test_locale():\n    assert True\n"}}]}
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan))
    return SimpleNamespace(root=root, store=store, tracker=tracker, git=git, events=events,
                           worker=worker, calls=calls, plan=plan, path=path, values=values)


def invoke(capsys, *argv):
    capsys.readouterr()
    try:
        code = cli.main(list(map(str, argv)))
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return SimpleNamespace(code=code, out=captured.out, err=captured.err,
                           text=captured.out + captured.err)


def enter(monkeypatch, capsys, identity):
    monkeypatch.setenv("FORAGENT_SESSION_ID", identity)
    return invoke(capsys)


def hook(case, monkeypatch, identity, *argv):
    payload = json.dumps({"session_id": identity, "cwd": str(case.root), "prompt": "Explain current work"})
    stdin = TextIOWrapper(BytesIO(payload.encode()), encoding="utf-8")
    stdout = TextIOWrapper(BytesIO(), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setenv("FORAGENT_SESSION_ID", identity)
        patch.setattr(sys, "stdin", stdin)
        patch.setattr(sys, "stdout", stdout)
        assert cli.main(["statusline", *argv]) == 0
        stdout.flush()
        return stdout.buffer.getvalue().decode()


def test_second_fa_takes_the_seat_and_old_pm_cannot_publish_or_approve(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    first = enter(monkeypatch, capsys, "pm-first")
    second = enter(monkeypatch, capsys, "pm-second")
    assert first.code == second.code == 0, "Bare fa must acquire the PM seat in this terminal"
    line = hook(case, monkeypatch, "pm-second")
    assert "PM" in line and "pm-second" in line, "Statusline must identify the current PM session"
    old_context = hook(case, monkeypatch, "pm-first", "--prompt-context")
    assert "PM seat moved to another terminal" in old_context
    monkeypatch.setenv("FORAGENT_SESSION_ID", "pm-first")
    before = (list(case.events), dict(case.tracker.issues))
    for flags in ([], ["--approve"]):
        result = invoke(capsys, "order", "publish", case.path, *flags)
        assert result.code != 0 and "PM seat moved to another terminal" in result.text, result.text
    assert (case.events, case.tracker.issues) == before, "A displaced PM must perform no publication writes"
    # A stale heartbeat/statusline must not take the seat back.
    invoke(capsys, "plan", "heartbeat")
    hook(case, monkeypatch, "pm-first")
    assert "pm-second" in hook(case, monkeypatch, "pm-second")
    monkeypatch.setenv("FORAGENT_SESSION_ID", "pm-second")
    assert invoke(capsys, "order", "preread", case.path, "--json").code == 0
    result = invoke(capsys, "order", "publish", case.path, "--approve")
    assert result.code == 0, result.text
    children = [i for i in case.tracker.list_issues() if i.title == "Keep locale"]
    assert len(children) == 1 and "approved" in children[0].labels


def test_a_third_terminal_can_take_over_without_killing_the_old_terminal(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    for identity in ("pm-first", "pm-second", "pm-third"):
        result = enter(monkeypatch, capsys, identity)
        assert result.code == 0, "Each explicit fa entrance must take the PM seat"
    for identity in ("pm-first", "pm-second"):
        assert "PM seat moved to another terminal" in hook(case, monkeypatch, identity, "--prompt-context")
    assert "pm-third" in hook(case, monkeypatch, "pm-third")
