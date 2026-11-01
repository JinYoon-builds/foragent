"""Locked acceptance checks for group-check-adaptive-limit, before #1141/#1142."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shlex
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from foragent import capacity, orch_events, run_baseline, run_group_check, run_merge_turn, runner
from foragent.config import Config
from foragent.issue import body_hash
from foragent.run_implementation import capture_acceptance_baseline
from foragent.state import StateStore


NODE = "tests/acceptance/test_keep.py::test_keep"


@pytest.mark.parametrize("measured,remaining,expected", [
    (736, 21600, 1472), (2000, 21600, 3600), (736, 1000, 1000), (200, 21600, 900),
])
def test_group_protected_check_uses_the_selected_baselines_adaptive_allowance(
    tmp_path, monkeypatch, measured, remaining, expected,
):
    epoch = datetime(2026, 11, 1, tzinfo=UTC)
    elapsed = [0.0]
    now = lambda: epoch + timedelta(seconds=elapsed[0])
    root = tmp_path / "repo"
    root.mkdir()
    store = StateStore(tmp_path / "state", now=now)
    for n in (1, 2):
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n))
        body = f"<!-- foragent:v1 -->\n## Done when\n```sh\necho member-{n}\n```\n"
        store.save_approval(n, body=body, body_sha=body_hash(body), acceptance_blobs={},
                            approved_by="human", approved_at=now().isoformat())
    checkouts, commits = {}, []

    def add_worktree(path, sha):
        checkouts[path] = sha
        test = path / NODE.split("::")[0]
        test.parent.mkdir(parents=True)
        test.write_text("def test_keep(): pass\n")

    def git_call(args):
        if args[:1] in (["status"], ["cat-file"]):
            return ""
        if args[:1] == ["merge-tree"]:
            return "union-tree\n"
        if "commit-tree" in args:
            commits.append(1)
            return f"combined-{len(commits)}"
        if args[:1] == ["log"]:
            return "tree-1\ntree-2\n"
        if args[:1] == ["diff"]:
            return "feature.txt\0"
        raise AssertionError(f"Unexpected fake Git request: {args}")

    git = SimpleNamespace(root=root, ref_sha=lambda ref: "base", blob_sha=lambda *args: None,
                          add_detached_worktree=add_worktree, remove_worktree=lambda path: None,
                          _call=git_call)
    git.at = lambda path: SimpleNamespace(head_sha=lambda: checkouts[path], _call=git_call)
    tracker = SimpleNamespace(find_pr=lambda branch: int(branch.rsplit("-", 1)[1]),
                              pr_head_sha=lambda pr: f"h{pr}")
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root, store=store, git=git, tracker=tracker,
                          now=now, monotonic=lambda: elapsed[0],
                          config=Config(repo="owner/repo", checks="local", test_command="pytest -q",
                                        merge_group_max=2, max_runtime_minutes=(measured + remaining + 59) // 60))
    # A real previous success permits a slow capture; it is not the group's selected baseline.
    cache = store.root.parent / "baselines"
    cache.mkdir()
    (cache / "history.json").write_text(json.dumps({
        "repository": str(root.resolve()), "duration_seconds": measured,
        "created_at": (epoch - timedelta(days=1)).isoformat(), "node_ids": [],
    }))
    captures, protected, commands = [], [], []

    @contextmanager
    def slot(*args, **kwargs):
        yield

    def process(argv, *, cwd, timeout_s, log_path, **kwargs):
        words = shlex.split(argv[-1])
        report = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
        if report is not None:
            captures.append(1)
            assert timeout_s >= measured
            elapsed[0] += measured
            suite = ET.Element("testsuite", time=str(measured))
            ET.SubElement(suite, "testcase", file=NODE.split("::")[0], name="test_keep", time=str(measured))
            ET.ElementTree(suite).write(report, encoding="unicode")
        else:
            commands.append(argv[-1])
            if NODE in words:
                protected.append(timeout_s)
                execution = min(1000, expected)
                elapsed[0] += min(execution, timeout_s)
                if timeout_s < execution:
                    return 143, True, "group protected check timed out"
        return 0, False, ""

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    monkeypatch.setattr(runner, "run_logged", process)
    monkeypatch.setattr(run_baseline, "monotonic", ctx.monotonic)
    baseline = capture_acceptance_baseline(1, ctx, git, "main", "main")
    assert baseline.node_ids == (NODE,) and baseline.duration_seconds == measured
    elapsed[0] = ctx.config.max_runtime_minutes * 60 - remaining
    for n in (1, 2):
        run_merge_turn.join(ctx, run_merge_turn.seat(ctx, n, "main"), f"h{n}")
    place, done = run_merge_turn.seat(ctx, 1, "main"), set()
    run_group_check.check(1, ctx, place, "h1", "base", done)
    run_group_check.check(1, ctx, place, "h1", "base", done)

    assert captures == [1], "The group must reuse the current base's exact measured selection"
    assert len(commands) == 3 and commands[:2] == ["echo member-1", "echo member-2"], "Run each check once"
    assert shlex.split(commands[2]).count(NODE) == 1, "Protect the exact selected node once"
    assert protected == [expected], (
        f"Group protected check must use the same adaptive allowance as a single run "
        f"({measured}s measurement, {remaining}s remaining): expected {expected}, got {protected}"
    )
    assert orch_events.has_event(store, 1, "group-check", passed=True, verdict="passed"), (
        "A protected check that fits the adaptive allowance must produce reusable passing group evidence"
    )
