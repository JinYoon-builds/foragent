"""Owner final checks consume only exact-head unit test receipts."""

import shlex
import sys
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME, git_command
from foragent import ci_mode, runner
from foragent.config import Config
from foragent.git import GitRepo
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from runner_support import import_index


@pytest.fixture
def owner_units(git_history, tmp_path, monkeypatch):
    root = git_history.clone
    path = "tests/test_receipt.py"
    source = (
        "from pathlib import Path\nimport pytest\n"
        "def record(name):\n"
        "    target = Path('.foragent/executed.txt')\n"
        "    target.parent.mkdir(exist_ok=True)\n"
        "    with target.open('a') as stream: stream.write(name + '\\n')\n"
        "def test_passed(): record('passed')\n"
        "def test_ci_skipped(): record('ci-skipped')\n"
        "def test_absent(): record('absent')\n"
        "@pytest.mark.local_only\n"
        "def test_machine(): record('machine')\n"
    )
    git_history.update({path: source})
    git_command(root, "fetch", "-q", "origin")
    git_command(root, "merge", "-q", "--ff-only", "origin/main")
    (root / path).write_text(source + "# Published owner head\n")
    git_command(root, "add", path)
    import_index(root, "Owner head")
    git = GitRepo(root)
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="test", worktree=root)
    tracker = FakeTracker()
    ctx = RunContext("owner/repo", root, tracker, store, Config(
        checks="ci", merge_owner=True, runner_unit_tests="all",
        test_command=f"{shlex.quote(sys.executable)} -m pytest"), git, lambda: BASE_TIME)
    calls = []
    check = runner._check

    def observed(command, *args, **kwargs):
        calls.append(command)
        return check(command, *args, **kwargs)

    monkeypatch.setattr(runner, "_check", observed)
    return SimpleNamespace(root=root, path=path, git=git, tracker=tracker, ctx=ctx,
                           store=store, calls=calls)


def executed(case):
    target = case.root / ".foragent/executed.txt"
    return target.read_text().splitlines() if target.exists() else []


def test_owner_review_skips_passed_but_runs_ci_skipped_absent_and_local_only(owner_units, monkeypatch):
    case = owner_units
    lookups = []

    def receipt(head):
        lookups.append(head)
        return 42, {f"{case.path}::test_passed"}

    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", receipt)
    ci_mode.review_checks(1, case.ctx, case.git, "main")

    assert lookups == [case.git.head_sha()]
    assert "passed" not in executed(case)
    assert {"ci-skipped", "absent", "machine"} <= set(executed(case))
    assert any("--collect-only" in command and case.path in command for command in case.calls)
    related = [command for command in case.calls if "--junitxml " in command]
    assert len(related) == 1
    assert case.path in shlex.split(related[0])
    assert "-p _foragent_residual_" in related[0]
    assert f"related units: skipped 1 of 4 using CI run 42 for {case.git.head_sha()}" in (
        case.store.log_path(1).read_text())


def test_owner_review_all_passed_still_runs_local_only_units(owner_units, monkeypatch):
    case = owner_units
    nodes = {f"{case.path}::{name}" for name in (
        "test_passed", "test_ci_skipped", "test_absent", "test_machine")}
    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", lambda head: (42, nodes))

    ci_mode.review_checks(1, case.ctx, case.git, "main")

    assert executed(case) == ["machine"]
    assert not any("--junitxml " in command for command in case.calls)
    assert f"related units: all 4 passed in CI run 42 for {case.git.head_sha()}" in (
        case.store.log_path(1).read_text())


@pytest.mark.parametrize("checks,owner", [("local", True), ("ci", False), ("ci", True)])
def test_unavailable_or_disabled_receipt_preserves_file_execution(owner_units, monkeypatch, checks, owner):
    from dataclasses import replace

    case = owner_units
    case.ctx.config = replace(case.ctx.config, checks=checks, merge_owner=owner)
    lookups = []

    def unavailable(head):
        lookups.append(head)
        return None

    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", unavailable)
    ci_mode.review_checks(1, case.ctx, case.git, "main")

    assert lookups == ([case.git.head_sha()] if checks == "ci" and owner else [])
    assert {"passed", "ci-skipped", "absent", "machine"} <= set(executed(case))
    related = [command for command in case.calls if "--junitxml " in command]
    assert len(related) == 1 and shlex.split(related[0])[-1] == case.path
    assert not any("--collect-only" in command and case.path in command for command in case.calls)
