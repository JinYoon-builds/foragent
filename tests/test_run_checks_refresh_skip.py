"""#2147: a plain post-PASS base refresh in CI mode leaves related units to PR CI.

The verification body is exercised with its collaborators replaced, so each
case shows exactly which checks start and which lines the log gets.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent import ci_local, ci_mode, harness_guard, run_checks, runner, run_conflicts, run_implementation, run_publish
from foragent.run_checks import CheckResult


def _ok(command: str = "x") -> CheckResult:
    return CheckResult(command=command, exit_code=0, timed_out=False, output="", seconds=0.0)


class FakeGit:
    def __init__(self, trees):
        self.trees = list(trees)

    def stage_changes(self):
        pass

    def changed_paths(self, ref):
        return ["src/demo.py"]

    def staged_tree_sha(self):
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]


@pytest.fixture
def world(tmp_path, monkeypatch):
    calls = {"related": 0, "local": 0, "done_when": 0}
    log = tmp_path / "issue.log"
    log.write_text("")
    state = SimpleNamespace(mode=True)

    def related(*args, **kwargs):
        calls["related"] += 1
        return _ok("related")

    def local(attempt, command):
        calls["local"] += 1
        return _ok("local")

    def done_when(*args, **kwargs):
        calls["done_when"] += 1
        return _ok("done")

    monkeypatch.setattr(run_checks, "_phase", lambda *a, **k: None)
    monkeypatch.setattr(run_checks, "_progress", lambda *a, **k: None)
    monkeypatch.setattr(run_checks, "_test_slot", lambda *a, **k: None)
    monkeypatch.setattr(run_checks, "run_done_when", done_when)
    monkeypatch.setattr(run_checks, "run_related_check", related)
    monkeypatch.setattr(run_checks, "_related_unit_tests", lambda *a, **k: ["tests/test_demo.py"])
    monkeypatch.setattr(run_checks, "save_failures", lambda *a, **k: "")
    monkeypatch.setattr(harness_guard, "guard_staged", lambda attempt: None)
    monkeypatch.setattr(run_conflicts, "_prepare_target", lambda attempt: True)
    monkeypatch.setattr(run_implementation, "follow_base", lambda attempt: None)
    monkeypatch.setattr(run_publish, "_check_publication", lambda *a, **k: None)
    monkeypatch.setattr(ci_mode, "enabled", lambda ctx, branch: state.mode)
    monkeypatch.setattr(ci_local, "verify_local_only_units", local)
    monkeypatch.setattr(runner, "_remaining", lambda n, ctx: 900)

    def attempt(*, to_ci: bool, trees=("t1",)):
        config = SimpleNamespace(test_command="pytest -q", acceptance_dir="tests/acceptance",
                                 runner_unit_tests="own")
        ctx = SimpleNamespace(config=config, store=SimpleNamespace(root=tmp_path), monotonic=lambda: 0.0)
        value = SimpleNamespace(
            n=1, ctx=ctx, git=FakeGit(trees), worktree=tmp_path, log=log, start_ref="base",
            parsed=SimpleNamespace(done_when_commands=["pytest tests/acceptance/test_x.py"]),
            baseline=None, target_branch="main", checks=[], paths=[],
        )
        if to_ci:
            value.related_to_ci = True
        return value

    return SimpleNamespace(calls=calls, log=log, attempt=attempt, state=state)


def _ci_lines(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if "PR CI" in line]


def test_marked_refresh_skips_related_units_and_logs_one_line(world):
    assert run_checks._verify_contents(world.attempt(to_ci=True)) == ""
    assert world.calls["related"] == 0
    assert world.calls["done_when"] == 1, "Done when still runs"
    assert world.calls["local"] == 1, "local_only units still run"
    assert _ci_lines(world.log) == ["Related unit tests (1 files) left to PR CI on the new head"]


def test_unmarked_check_runs_related_units_locally(world):
    assert run_checks._verify_contents(world.attempt(to_ci=False)) == ""
    assert world.calls["related"] == 1
    assert _ci_lines(world.log) == []


def test_two_verification_passes_still_log_one_line(world):
    # Done when changed the staged tree once: the body checks the final contents again.
    attempt = world.attempt(to_ci=True, trees=("t1", "t2", "t2"))
    assert run_checks._verify_contents(attempt) == ""
    assert world.calls["done_when"] == 2
    assert world.calls["related"] == 0
    assert len(_ci_lines(world.log)) == 1


# --- Which head counts as post-PASS (run_publish_flow.reviewed) ---------------------------------

from datetime import UTC, datetime  # noqa: E402

from foragent import orch_events, run_publish_flow  # noqa: E402
from foragent.state import StateStore  # noqa: E402


class TreeGit:
    """Answers `rev-parse <head>^{tree}` from a fixed map of commits to trees."""

    def __init__(self, trees):
        self.trees = trees

    def _call(self, args):
        assert args[0] == "rev-parse"
        return self.trees[args[1].removesuffix("^{tree}")] + "\n"


@pytest.fixture
def pass_world(tmp_path):
    store = StateStore(tmp_path / "state")
    store.save_approval(1, body="b", body_sha="body-1", acceptance_blobs={}, approved_by="jin",
                        approved_at="2026-11-05T00:00:00Z")
    ctx = SimpleNamespace(store=store)
    git = TreeGit({"head": "tree-head", "merged": "tree-merged"})

    def review(verdict, *, tree="tree-head", body_sha="body-1", source="reviewer", issue=1):
        orch_events.append_event(store, datetime.now(UTC), issue, "review", verdict, verdict=verdict,
                                 source=source, tree=tree, body_sha=body_sha, round=1)

    return SimpleNamespace(ctx=ctx, git=git, review=review)


def test_no_review_is_not_a_pass(pass_world):
    # reviewer: none records no review: the base refresh keeps the local related run.
    assert run_publish_flow.reviewed(1, pass_world.ctx, pass_world.git, "head") is False


def test_reviewer_pass_of_this_tree_counts(pass_world):
    pass_world.review("PASS")
    assert run_publish_flow.reviewed(1, pass_world.ctx, pass_world.git, "head") is True


@pytest.mark.parametrize("kwargs", [
    {"verdict": "FAIL"},
    {"verdict": "PASS", "tree": "tree-merged"},
    {"verdict": "PASS", "body_sha": "older-body"},
    {"verdict": "PASS", "source": "orchestrator"},
    {"verdict": "PASS", "issue": 2},
], ids=["fail", "other-tree", "other-body", "orchestrator", "other-issue"])
def test_only_a_reviewer_pass_of_this_tree_and_body_counts(pass_world, kwargs):
    pass_world.review(**kwargs)
    assert run_publish_flow.reviewed(1, pass_world.ctx, pass_world.git, "head") is False
