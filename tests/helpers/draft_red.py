"""Shared offline helpers for unit tests."""

import json
import sys

import pytest
import yaml

from conftest import git_command, git_history
from foragent.git import GitRepo
from foragent.tracker import FakeTracker


DRAFT = "tests/acceptance/test_greeting.py"
_MAIN_GREETING = "def greet():\n    return ''\n"
DIRTY_GREETING = "def greet():\n    return 'hello'\n"
RED = "from greeting import greet\n\n\ndef test_greets_the_person():\n    assert greet() == 'hello'\n"
_MUTATIONS = (
    "ensure_labels", "create_issue", "update_issue", "add_labels", "remove_labels",
    "comment", "close_issue", "add_sub_issue", "add_blocked_by", "remove_blocked_by",
)


@pytest.fixture
def draft_case(git_history, tmp_path, monkeypatch):
    from foragent import cli, executors, gh

    history, root = git_history, git_history.clone
    settings = {
        "repo": "owner/repo", "language": "en", "skill_libraries": [], "test_slots": 1,
        "test_command": f"{sys.executable} -m pytest -q",
    }
    history.update({
        ".foragent.yml": yaml.safe_dump(settings),
        "pyproject.toml": "[tool.pytest.ini_options]\npythonpath = [\".\"]\n",
        "greeting.py": _MAIN_GREETING,
        # Running anything but the draft's own files fails loudly.
        "tests/test_unrelated.py": "raise RuntimeError('unrelated files must not run')\n",
    })
    GitRepo(root).update_base("main")
    (root / "greeting.py").write_text(DIRTY_GREETING)

    tracker, mutations, models = FakeTracker(actor="human"), [], []
    for name in _MUTATIONS:
        original = getattr(tracker, name)

        def mutate(*args, _name=name, _original=original, **kwargs):
            mutations.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(tracker, name, mutate)

    def no_model(*args, **kwargs):
        models.append(args)
        pytest.fail("Checking a draft must not start a model")

    def no_github(*args, **kwargs):
        pytest.fail("Checking a draft must not call GitHub")

    monkeypatch.setattr(executors, "executor_for", no_model)
    if hasattr(cli, "executor_for"):
        monkeypatch.setattr(cli, "executor_for", no_model)
    monkeypatch.setattr(gh, "run", no_github)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.chdir(root)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    plan_path = tmp_path / "draft" / "plan.json"
    plan_path.parent.mkdir()
    return root, history, plan_path, mutations, models


def draft(files):
    """A lane's draft for decided issue #41: its tests replace the placeholder Done when."""
    return {
        "id": "draft-41", "title": "Greet the person", "summary": "Tests for decided issue #41.",
        "spec_markdown": "# Greet the person\nThe greeting says hello.\n",
        "issues": [],
        "edits": [{
            "number": 41, "goal": "The greeting says hello.", "out_of_scope": "Other greetings.",
            "done_when": [f"uv run pytest -q {name}" for name in files],
            "done_when_removals": [{
                "line": "false  # placeholder: acceptance tests are written before this issue can be approved",
                "reason": "The lane's tests replace the placeholder."}],
            "checklist": [], "files": ["greeting.py"], "acceptance_files": files,
        }],
    }


def _snapshot(root, history):
    return {
        "status": git_command(root, "status", "--porcelain", "--untracked-files=all"),
        "index": git_command(root, "ls-files", "--stage"),
        "head": git_command(root, "rev-parse", "HEAD"),
        "origin": git_command(history.origin, "rev-parse", "refs/heads/main"),
        "greeting": (root / "greeting.py").read_text(),
    }


def check(case, capsys, files):
    from foragent import cli

    root, history, plan_path, mutations, models = case
    plan_path.write_text(json.dumps(draft(files)))
    before = _snapshot(root, history)
    capsys.readouterr()
    try:
        code = cli.main(["order", "red", str(plan_path), "--repo-root", str(root)])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert _snapshot(root, history) == before, "Checking a draft changed the checkout, its index or main"
    for name in files:
        assert not (root / name).exists(), f"The draft file {name} was written into the checkout"
    assert mutations == [] and models == [], "Checking a draft wrote to GitHub or started a model"
    return code, output
