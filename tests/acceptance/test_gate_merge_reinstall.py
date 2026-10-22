"""Acceptance test for gate-merge-reinstall. Locked by Foragent; do not edit."""

from pathlib import Path

from test_skills_autosync import isolated_home


def fast_self_merge(git_history, tmp_path, monkeypatch, project_name, *, install_mode="match"):
    from conftest import git_command
    from foragent.runner import run_issue
    from test_orch_reinstall import PR_VERSION
    from test_orch_review_handoff import make_case
    from test_view_open import fake_commands

    commands = fake_commands(tmp_path, monkeypatch)
    monkeypatch.setenv("FA_D57_INSTALL_MODE", install_mode)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    git_history.update({"pyproject.toml": f'[project]\nname = "{project_name}"\n',
                        "src/foragent/__init__.py": "BUILD = 'old base version'\n"})
    case = make_case(git_history, tmp_path, monkeypatch, review="fast", test_command="true",
                     skill_libraries=[])
    merge = case.tracker.merge_pr

    def implement(kwargs):
        case.implement(kwargs)
        (kwargs["worktree"] / "src/foragent/__init__.py").write_text(PR_VERSION)

    case.implementer.steps = [implement]

    def merged(number, *, expected_head_sha, method="squash"):
        merge(number, expected_head_sha=expected_head_sha, method=method)
        git_command(git_history.origin, "update-ref", "refs/heads/main", expected_head_sha)
        git_history.tip = expected_head_sha

    monkeypatch.setattr(case.tracker, "merge_pr", merged)
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.tracker.merged_prs) == 1
    return case, commands


def test_gate_merge_reinstall_fast_merge_installs_merged_tree_once_and_checks_copy(
    git_history, tmp_path, monkeypatch,
):
    from test_orch_reinstall import PR_VERSION, install_calls

    case, commands = fast_self_merge(git_history, tmp_path, monkeypatch, "foragent")
    calls = install_calls(commands)
    assert len(calls) == 1, "fast gate self-merge must reinstall exactly once"
    call = calls[0]
    assert call["argv"][:4] == ["tool", "install", "--force", "--reinstall"]
    assert call["source_init"] == PR_VERSION
    assert Path(call["source_root"]).resolve() != case.root.resolve()
    installed = commands.tool_dir / "foragent/lib/python3.12/site-packages/foragent/__init__.py"
    assert installed.read_text() == PR_VERSION
    assert any(row["argv"] == ["tool", "dir"] for row in commands.calls("uv"))


def test_gate_merge_reinstall_other_project_never_installs(git_history, tmp_path, monkeypatch):
    case, commands = fast_self_merge(git_history, tmp_path, monkeypatch, "other-project")
    assert commands.calls("uv") == []
    assert case.store.get_run(1).outcome == "merged"


def test_gate_merge_reinstall_mismatching_install_is_reported_after_successful_merge(
    git_history, tmp_path, monkeypatch,
):
    import json

    from test_orch_reinstall import install_calls

    case, commands = fast_self_merge(git_history, tmp_path, monkeypatch, "foragent", install_mode="mismatch")
    assert len(install_calls(commands)) == 1
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "reinstall-failed" for event in events)
    assert case.store.get_run(1).outcome == "merged"
