"""Offline project and session setup for order-job unit tests."""

import json
from types import SimpleNamespace

import yaml

from foragent import cli, executors, order
from foragent.executors import ExecResult
from foragent.state import StateStore
from helpers.cli_invoke import invoke
from test_order import PublishingGit, PublishingTracker


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
    events, calls = [], []
    tracker = PublishingTracker(events)  # Records ("issue", title) on each issue creation.
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


def enter(monkeypatch, capsys, identity):
    monkeypatch.setenv("FORAGENT_SESSION_ID", identity)
    return invoke(capsys)
