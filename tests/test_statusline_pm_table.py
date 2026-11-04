"""The PM table follows parser registrations and stays independent of the summary."""

import argparse
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from foragent import presence, statusline
from foragent.commands import _pm_table


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    store = SimpleNamespace(root=tmp_path / "state")
    monkeypatch.setattr(presence, "_project", lambda folder: (None, store))
    monkeypatch.setattr(presence, "record_terminal", lambda *args: None)
    monkeypatch.setattr(presence, "bind_checkout", lambda *args: None)
    monkeypatch.setattr(presence, "session_start_message", lambda *args: "original summary")
    monkeypatch.setattr(statusline, "load_record", lambda: None)
    monkeypatch.setattr(statusline, "_export_session", lambda *args: None)

    def invoke(folder=tmp_path):
        output = BytesIO()
        assert statusline.session_start(BytesIO(json.dumps({"cwd": str(folder)}).encode()), output) == 0
        return json.loads(output.getvalue()) if output.getvalue() else {}

    return invoke, store


def test_summary_off_still_gets_one_table(hook, monkeypatch):
    invoke, _ = hook
    monkeypatch.setattr(presence, "session_start_message", lambda *args: None)
    output = invoke()
    assert "systemMessage" not in output
    context = output["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert context["additionalContext"].count("`fa gate watch --open`") == 1


@pytest.mark.parametrize("role", ["", "launched", "orchestrator"])
def test_every_role_keeps_only_original_summary(hook, monkeypatch, role):
    invoke, _ = hook
    monkeypatch.setenv("FORAGENT_ROLE", role)
    assert invoke() == {"systemMessage": "original summary"}


def test_worktree_and_missing_project_get_no_table(hook, monkeypatch):
    invoke, store = hook
    assert invoke(store.root / "worktrees" / "issue-1") == {"systemMessage": "original summary"}
    monkeypatch.setattr(presence, "_project", lambda folder: None)
    assert invoke() == {"systemMessage": "original summary"}
    monkeypatch.setattr(presence, "_project", lambda folder: (None, None))
    assert invoke() == {"systemMessage": "original summary"}


def test_table_failure_preserves_summary(hook, monkeypatch):
    invoke, _ = hook
    def broken():
        raise RuntimeError("parser unavailable")
    monkeypatch.setattr(_pm_table, "render", broken)
    assert invoke() == {"systemMessage": "original summary"}


def test_option_rename_and_removal_follow_registered_actions(monkeypatch):
    original = argparse.ArgumentParser.add_argument
    remove = False

    def add_argument(self, *args, **kwargs):
        marked = args == ("--open",)
        if marked:
            args = ("--show",)
        action = original(self, *args, **kwargs)
        if marked and remove:
            self._actions.remove(action)
        return action

    monkeypatch.setattr(argparse.ArgumentParser, "add_argument", add_argument)
    context = _pm_table.render()
    assert "`fa gate watch --show`" in context
    assert "--open" not in context
    remove = True
    assert "open the dashboard" not in _pm_table.render()
