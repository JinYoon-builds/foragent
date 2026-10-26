"""Acceptance test for view-open-triggers. Locked by Foragent; do not edit."""

import json
import os

import pytest


def foreground_case(git_history, tmp_path, monkeypatch):
    from foragent import cli, lanes
    from foragent.config import load_config
    from foragent.init import init_repo
    from test_view_open import view_case

    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_test1")
    # Foreground publication uses the same human/non-strict mode as test_order's tracker.
    case.tracker.actor = "human"
    case.tracker.strict = False
    # lanes/init construct their own Git adapter; share the existing offline runner.
    monkeypatch.setattr(lanes, "GitRepo", lambda root: case.git)
    local_config = lambda root: load_config(root, tmp_path / "no-global.yml")
    monkeypatch.setattr(lanes, "load_config", local_config)
    monkeypatch.setattr("foragent.init.load_config", local_config)
    monkeypatch.setattr(case.git, "remote_slug", lambda: "owner/repo")
    monkeypatch.setattr(cli, "init_repo", lambda root, **kw: init_repo(
        root, git=case.git, tracker=case.tracker, gh_run=lambda *a, **kw: "human", **kw))
    from test_publish_own_paths import with_clean_preread

    return case, with_clean_preread(main, monkeypatch), commands


def foreground_argv(case, command, sequence=1):
    if command == "lanes":
        return ["lanes", "add", f"lane-{sequence}", "--paths", f"area-{sequence}/**"]
    if command == "init":
        # test_board_projects.initialized_case already drives this real init path.
        return ["init", "--no-push", "--no-launchd", "--no-board", "--approver", "human"]
    if command == "orch":
        from foragent.actions import flag
        from foragent.reasons import Reason

        # Publish through Foragent's write path so the next watch sees a new
        # local blocker immediately, without depending on a gate refresh.
        case.tracker.add_issue(100 + sequence, "Waiting for repair")
        flag(case.ctx, 100 + sequence, Reason.RETRIES_EXCEEDED, "Waiting for repair")
        return ["orch", "watch", "--interval", "0.01"]
    path = case.root / f"plan-{sequence}.json"
    acceptance = f"tests/acceptance/test_order_view_{sequence}.py"
    path.write_text(json.dumps({
        "id": f"view-{sequence}", "title": "Open the lane view", "summary": "A foreground order",
        "spec_markdown": "# View order\n",
        "issues": [{"key": "view", "title": "View task", "goal": "Show progress.",
                    "out_of_scope": "Other work.", "lane": "main", "size": "s",
                    "done_when": [f"uv run pytest {acceptance}"],
                    "acceptance_files": {acceptance: "def test_view():\n    assert True\n"}}],
    }))
    return ["order", "publish", str(path), "--approve"]


@pytest.mark.parametrize("command", ["lanes", "init"])
def test_successful_lane_add_or_init_opens_exactly_one_split(
    git_history, tmp_path, monkeypatch, command,
):
    from test_view_open import assert_opened

    case, main, commands = foreground_case(git_history, tmp_path, monkeypatch)
    assert main(foreground_argv(case, command)) == 0
    if command == "lanes":
        assert (case.root / ".claude/agents/fa-lane-1.md").is_file()
    else:
        assert (case.root / ".github/ISSUE_TEMPLATE/foragent-task.md").is_file()
    assert_opened(commands, case.root, terminal="term_test1")


def test_publish_dry_run_stays_closed_then_approved_publication_opens_once(
    git_history, tmp_path, monkeypatch,
):
    from test_view_open import assert_opened

    case, main, commands = foreground_case(git_history, tmp_path, monkeypatch)
    argv = foreground_argv(case, "publish")
    before = dict(case.tracker.issues)
    assert main([*argv, "--dry-run"]) == 0
    assert commands.calls() == [] and case.tracker.issues == before
    assert main(argv) == 0
    child = next(issue for issue in case.tracker.list_open_issues() if issue.title == "View task")
    assert "approved" in child.labels
    assert_opened(commands, case.root, terminal="term_test1")


def test_orchestrator_watch_preserves_live_view_then_opens_when_it_is_gone(
    git_history, tmp_path, monkeypatch,
):
    from test_view_open import assert_opened

    case, main, commands = foreground_case(git_history, tmp_path, monkeypatch)
    pid_file = case.store.root / "watch.pid"
    pid_file.write_text(str(os.getpid()))
    assert main(foreground_argv(case, "orch")) == 0
    assert commands.calls() == []
    pid_file.unlink()
    assert main(foreground_argv(case, "orch", sequence=2)) == 0
    assert_opened(commands, case.root, terminal="term_test1")


@pytest.mark.parametrize("command", ["lanes", "publish", "orch", "init"])
def test_foreground_opt_out_then_unset_opens_the_view(
    git_history, tmp_path, monkeypatch, command,
):
    from test_view_open import assert_opened

    case, main, commands = foreground_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    assert main(foreground_argv(case, command)) == 0
    assert commands.calls() == []
    # A positive control keeps every parameter red while the feature is absent.
    monkeypatch.delenv("FORAGENT_VIEW", raising=False)
    assert main(foreground_argv(case, command, sequence=2)) == 0
    assert_opened(commands, case.root, terminal="term_test1")
