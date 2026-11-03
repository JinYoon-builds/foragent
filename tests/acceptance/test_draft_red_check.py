"""Order acceptance: one command tells whether a lane's test draft is red on clean main for a real reason.

A draft is a plan whose acceptance files a lane wrote for a decided issue. The check runs those
files against the base branch as published (not the caller's working tree) and accepts the draft
only when every collected test fails there by a real failure. A test that already passes, a file
that collects nothing, a collection error and a failure caused by a missing module are not red.

Only GitHub and models are faked; Git history and the test runs are real.
"""

import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command, git_history  # noqa: E402, F401
from foragent.git import GitRepo  # noqa: E402
from foragent.tracker import FakeTracker  # noqa: E402

DRAFT = "tests/acceptance/test_greeting.py"
SECOND = "tests/acceptance/test_greeting_more.py"
MAIN_GREETING = "def greet():\n    return ''\n"
# The caller's uncommitted work already implements the feature; main does not.
DIRTY_GREETING = "def greet():\n    return 'hello'\n"
RED = "from greeting import greet\n\n\ndef test_greets_the_person():\n    assert greet() == 'hello'\n"
MUTATIONS = (
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
        "greeting.py": MAIN_GREETING,
        # Running anything but the draft's own files fails loudly.
        "tests/test_unrelated.py": "raise RuntimeError('unrelated files must not run')\n",
    })
    GitRepo(root).update_base("main")
    (root / "greeting.py").write_text(DIRTY_GREETING)

    tracker, mutations, models = FakeTracker(actor="human"), [], []
    for name in MUTATIONS:
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


def snapshot(root, history):
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
    before = snapshot(root, history)
    capsys.readouterr()
    try:
        code = cli.main(["order", "red", str(plan_path), "--repo-root", str(root)])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert snapshot(root, history) == before, "Checking a draft changed the checkout, its index or main"
    for name in files:
        assert not (root / name).exists(), f"The draft file {name} was written into the checkout"
    assert mutations == [] and models == [], "Checking a draft wrote to GitHub or started a model"
    return code, output


def test_a_draft_that_fails_on_clean_main_by_an_assertion_is_red(draft_case, capsys):
    code, output = check(draft_case, capsys, {DRAFT: RED, SECOND: RED.replace("person", "visitor")})
    # The caller's dirty greeting.py would make these pass; only main decides.
    assert code == 0, output


NOT_RED = {
    "already_passes": (
        {DRAFT: "from greeting import greet\n\n\ndef test_quiet_greeting():\n    assert greet() == ''\n"},
        "test_quiet_greeting"),
    "missing_module_in_test": (
        {DRAFT: "def test_waves():\n    from greeting_wave import wave\n    assert wave() == 'hi'\n"},
        "greeting_wave"),
    "missing_module_at_import": (
        {DRAFT: "from greeting_wave import wave\n\n\ndef test_waves():\n    assert wave() == 'hi'\n"},
        "greeting_wave"),
    "nothing_collected": ({DRAFT: "GREETING = 'hello'\n"}, DRAFT),
    "one_test_already_passes": (
        {DRAFT: RED + "\n\ndef test_quiet_greeting():\n    from greeting import greet\n"
                      "    assert greet() == ''\n"},
        "test_quiet_greeting"),
    "one_test_misses_a_module": (
        {DRAFT: RED + "\n\ndef test_waves():\n    from greeting_wave import wave\n    assert wave() == 'hi'\n"},
        "greeting_wave"),
    "second_file_already_passes": (
        {DRAFT: RED, SECOND: "def test_second_quiet():\n    assert True\n"},
        "test_second_quiet"),
}


@pytest.mark.parametrize("case", sorted(NOT_RED))
def test_a_draft_that_is_not_red_for_a_real_reason_is_refused_and_named(draft_case, capsys, case):
    files, named = NOT_RED[case]
    code, output = check(draft_case, capsys, files)
    assert code != 0, f"{case}: a draft that is not red for a real reason was accepted:\n{output}"
    assert named in output, f"{case}: the refusal must name {named!r}:\n{output}"
