"""Locked order tests: base failures are reported; new related failures block."""

from pathlib import Path
import sys

import pytest


ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".foragent.yml").is_file())
# The staged order lives outside tests/. After publication pytest loads the same
# repository fixture plugin normally. This imports test support, never runner internals.
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests/acceptance"))
pytest_plugins = [] if "conftest" in sys.modules else ["conftest"]

from test_acceptance_baseline import make_case  # noqa: E402


UNIT_FILE = "tests/test_related_feature.py"
OLD = "test_already_red"
REGRESSION = "test_passed_on_base"
NEW = "test_new_on_head"
UNIT_SOURCE = f"""from pathlib import Path

def {OLD}():
    assert False, 'existing base failure'

def {REGRESSION}():
    assert Path('keep.txt').is_file(), 'new head regression'
"""


def _case(git_history, tmp_path, monkeypatch, *, retries):
    from conftest import git_command

    git_history.update({UNIT_FILE: UNIT_SOURCE, "keep.txt": "keep\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    return make_case(
        git_history, tmp_path, monkeypatch,
        test_command=f"{sys.executable} -m pytest -q", max_retries=retries, runner_unit_tests="all",
    )


def _select_related_file(case, kwargs):
    case.implement(kwargs)
    path = kwargs["worktree"] / UNIT_FILE
    path.write_text(path.read_text(encoding="utf-8") + "\n# feature work\n", encoding="utf-8")


def _report(case):
    return case.store.log_path(1).read_text(encoding="utf-8")


def test_related_failure_already_on_base_is_reported_without_blocking(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = _case(git_history, tmp_path, monkeypatch, retries=0)
    case.implementer.steps = [lambda kwargs: _select_related_file(case, kwargs)]
    outcome = run_issue(1, case.ctx)
    assert outcome == "merged", f"base-failing related test blocked the run: {outcome}\n{_report(case)}"
    assert len(case.implementer.calls) == 1
    assert OLD in _report(case), "an exempted base failure must remain reported"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert case.store.get_run(1).related_tests, "related verification must actually run"


def test_a_new_failure_in_the_same_file_as_a_base_failure_still_requires_repair(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case = _case(git_history, tmp_path, monkeypatch, retries=1)

    def break_keep(kwargs):
        _select_related_file(case, kwargs)
        (kwargs["worktree"] / "keep.txt").unlink()

    def repair(kwargs):
        (kwargs["worktree"] / "keep.txt").write_text("keep\n", encoding="utf-8")

    case.implementer.steps = [break_keep, repair]
    outcome = run_issue(1, case.ctx)
    assert outcome == "merged", f"repair did not clear the related regression: {outcome}\n{_report(case)}"
    assert len(case.implementer.calls) == 2, "a new failure must reach the repair turn before merge"
    assert REGRESSION in case.implementer.calls[1]["prompt"]
    assert OLD in _report(case)
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def test_a_failing_related_test_created_on_head_is_not_a_base_failure(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = _case(git_history, tmp_path, monkeypatch, retries=0)

    def introduce(kwargs):
        _select_related_file(case, kwargs)
        path = kwargs["worktree"] / UNIT_FILE
        path.write_text(path.read_text(encoding="utf-8") + f"\ndef {NEW}():\n    assert False\n",
                        encoding="utf-8")

    case.implementer.steps = [introduce]
    assert run_issue(1, case.ctx) != "merged"
    assert NEW in _report(case)
    assert case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
