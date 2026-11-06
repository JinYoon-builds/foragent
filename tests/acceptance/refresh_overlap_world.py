"""Shared world for refresh-recheck-overlap-only acceptance tests.

Real runner, Git, pytest selection and JUnit reports; only GitHub, models and
the clock are fake (the ci-mode world of test_ci_mode, extended).

The issue changes two source files, each with its own unit test file:
- src/demo.py   -> tests/test_demo.py
- src/extra.py  -> tests/test_extra.py
src/demo.py is long enough that main can change its last line while the issue
changes its first line, so the base merges in without a conflict.

Main moves after the reviewer PASS (during review) and, optionally, again
while publication waits for PR CI on the refreshed head. Each move either
touches only src/neighbor.py (a file the issue does not change) or the tail of
src/demo.py (a file the issue changes).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from conftest import GitHistory, git_command
from foragent import runner
from test_ci_mode import OWN, mode_case

DEMO_TEST = OWN  # tests/test_demo.py::test_demo
EXTRA_TEST = "tests/test_extra.py::test_extra"
RELATED = (DEMO_TEST, EXTRA_TEST)

PAD = "".join(f"# filler line {i}\n" for i in range(12))
BASE_DEMO = "VALUE = 1\n" + PAD + "TAIL = 'base'\n"
ISSUE_DEMO = "VALUE = 2\n" + PAD + "TAIL = 'base'\n"


def neighbor_move(tag):
    return {"src/neighbor.py": f"OTHER = 'main moved: {tag}'\n"}


def demo_move(tag):
    """Main edits the last line of src/demo.py, a file this issue also changes."""
    return {"src/demo.py": "VALUE = 1\n" + PAD + f"TAIL = 'main {tag}'\n"}


def build_case(git_history, tmp_path, monkeypatch, *, mode="ci"):
    case = mode_case(git_history, tmp_path, monkeypatch, mode=mode)
    case.history.update({
        "src/demo.py": BASE_DEMO,
        "src/extra.py": "EXTRA = 1\n",
        "tests/test_extra.py": "from src.extra import EXTRA\n\ndef test_extra():\n    assert EXTRA > 0\n",
    })
    git_command(case.history.clone, "fetch", "-q", "origin")
    git_command(case.history.clone, "merge", "-q", "--ff-only", "origin/main")
    case.base_sha = case.history.tip

    original = case.implement

    def implement(kwargs):
        original(kwargs)
        (kwargs["worktree"] / "src/demo.py").write_text(ISSUE_DEMO)
        (kwargs["worktree"] / "src/extra.py").write_text("EXTRA = 2\n")
        return "Implemented demo and extra; own tests are tests/test_demo.py and tests/test_extra.py"

    case.implement = implement
    case.implementer.steps = [implement]
    return case


def schedule_moves(case, first, second=None):
    """Move main once during the reviewer PASS and, if given, once more while
    publication waits for CI on the pushed head that merged the first move.

    case.marks records the number of local process calls before each move and
    the main commit each move created."""
    case.marks = {}

    def review(kwargs):
        if "first" not in case.marks:
            case.marks["first"] = len(case.processes.calls)
            case.history.update(first)
            case.base_sha = case.marks["first_tip"] = case.history.tip
        return "VERDICT: PASS"

    case.reviewer.steps = [review] * 8
    if second is None:
        return

    def maybe_move_again():
        if "first" in case.marks and "second" not in case.marks:
            pushed = pushed_head(case)
            if pushed and carries(case, pushed, first):
                case.marks["second"] = len(case.processes.calls)
                case.marks["first_head"] = pushed
                case.history.update(second)
                case.base_sha = case.marks["second_tip"] = case.history.tip

    legacy = case.ci.legacy.command
    runs_by_head = case.ci.runs_by_head

    def legacy_command(args, input=None):
        maybe_move_again()
        return legacy(args, input)

    def moving_runs_by_head():
        maybe_move_again()
        return runs_by_head()

    case.ci.legacy.command = legacy_command
    case.ci.runs_by_head = moving_runs_by_head


def pushed_head(case):
    try:
        return git_command(case.history.origin, "rev-parse", "--verify", "-q", "refs/heads/foragent/issue-1")
    except subprocess.CalledProcessError:
        return None


def carries(case, ref, move):
    """Whether ``ref`` on origin carries main's change from ``move``: the last line
    of each moved file (the issue's own first-line change may sit beside it)."""
    for path, text in move.items():
        try:
            content = git_command(case.history.origin, "show", f"{ref}:{path}")
        except subprocess.CalledProcessError:
            return False
        if text.strip().splitlines()[-1] not in content.splitlines():
            return False
    return True


def is_ancestor(case, ancestor, ref):
    """Whether commit ``ancestor`` is in the history of ``ref`` on origin."""
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, ref], cwd=case.history.origin)
    return result.returncode == 0


def head_runs(case, calls):
    """Test runs on the implementation worktree (not baseline captures elsewhere)."""
    return [call for call in calls if call.cwd == case.store.worktree_path(1) and not call.capture]


def executed(calls):
    return set().union(*(call.executed for call in calls)) if calls else set()


def pushed_file(case, path):
    return git_command(case.history.origin, "show", f"foragent/issue-1:{path}")


def copy_history(git_history, root):
    root.mkdir()
    origin = shutil.copytree(git_history.origin, root / "origin.git")
    clone = shutil.copytree(git_history.clone, root / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    return GitHistory(origin, clone, git_history.tip)


def local_mode_world(git_history, tmp_path, first, second=None):
    """The same scenario with CI mode off, in its own copy of the offline history."""
    history = copy_history(git_history, tmp_path / "local-mode")
    with pytest.MonkeyPatch.context() as patch:
        case = build_case(history, tmp_path / "local-mode", patch, mode="local")
        schedule_moves(case, first, second)
        outcome = runner.run_issue(1, case.ctx)
        return case, outcome
