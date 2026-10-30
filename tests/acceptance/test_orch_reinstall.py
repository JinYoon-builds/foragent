"""Acceptance test for orch-reinstall. Locked by Foragent; do not edit."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import psutil
import pytest


PR_VERSION = "BUILD = 'merged PR version'\n"


def fake_commands(tmp_path, monkeypatch):
    """Keep the shared command log, with uv respecting private install paths."""
    from test_view_open import fake_commands as view_commands

    commands = view_commands(tmp_path, monkeypatch)
    bin_dir = tmp_path / "tool-bin"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_TOOL_DIR", str(commands.tool_dir))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(()))
    recorded_run = subprocess.run

    def run(argv, *args, **kwargs):
        if Path(argv[0]).name != "uv":
            return recorded_run(argv, *args, **kwargs)
        arguments = list(argv[1:])
        row = {"executable": "uv", "argv": arguments,
               "cwd": str(Path(kwargs.get("cwd") or Path.cwd()).resolve())}
        installing = arguments[:2] == ["tool", "install"]
        if installing:
            assert arguments[:-1] in (["tool", "install", "--force"],
                                      ["tool", "install", "--force", "--reinstall"])
            source = Path(arguments[-1]).resolve()
            row.update(source_root=str(source), source_init=(source / "src/foragent/__init__.py").read_text())
        with commands.path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        if arguments in (["tool", "dir"], ["tool", "dir", "--bin"]):
            directory = bin_dir if "--bin" in arguments else commands.tool_dir
            return subprocess.CompletedProcess(argv, 0, stdout=str(directory), stderr="")
        assert installing, argv
        mode = os.environ["FA_D57_INSTALL_MODE"]
        if mode == "error":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="offline installer failed")
        environment = kwargs.get("env") or os.environ
        commands.tool_dir = Path(environment.get("UV_TOOL_DIR", str(commands.tool_dir)))
        destination = commands.tool_dir / "foragent/lib/python3.12/site-packages/foragent"
        destination.mkdir(parents=True, exist_ok=True)
        if mode == "match":
            shutil.copytree(source / "src/foragent", destination, dirs_exist_ok=True)
        else:
            (destination / "__init__.py").write_text("BUILD = 'wrong installed version'\n")
        installed_bin = Path(environment.get("UV_TOOL_BIN_DIR", str(bin_dir)))
        installed_bin.mkdir(parents=True, exist_ok=True)
        for name in ("fa", "foragent"):
            (installed_bin / name).write_text("installed")
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return commands


def update_pr_tree(case, pr, head, files):
    from conftest import git_command

    base = git_command(case.history.origin, "rev-parse", "refs/heads/main")
    git_command(case.history.origin, "update-ref", "refs/heads/main", head)
    case.history.tip = head
    case.history.update(files)
    head = case.history.tip
    branch = next(item.head for item in case.tracker.list_prs() if item.number == pr)
    git_command(case.history.origin, "update-ref", f"refs/heads/{branch}", head)
    git_command(case.history.origin, "update-ref", "refs/heads/main", base)
    case.history.tip = base
    case.tracker.set_pr_head_sha(pr, head)
    case.git.fetch()
    return head


def reinstall_case(git_history, tmp_path, monkeypatch, *, project_name="foragent"):
    from conftest import git_command
    from test_orch_merge import premerge_case

    # The checkout remains older than these commits: it cannot be the install source.
    git_history.update({"src/foragent/__init__.py": "BUILD = 'old base version'\n"})
    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch)
    files = {"src/foragent/__init__.py": PR_VERSION}
    if project_name is not None:
        files["pyproject.toml"] = f'[project]\nname = "{project_name}"\n'
    head = update_pr_tree(case, pr, head, files)
    merge = case.tracker.merge_pr

    def merged(number, *, expected_head_sha, method="squash", expected_base_sha=None):
        result = merge(number, expected_head_sha=expected_head_sha, method=method,
              **({"expected_base_sha": expected_base_sha} if expected_base_sha is not None else {}))
        # FakeTracker records the merge; also make origin's merged base contain it.
        git_command(case.history.origin, "update-ref", "refs/heads/main", expected_head_sha)
        case.history.tip = expected_head_sha
        return result

    monkeypatch.setattr(case.tracker, "merge_pr", merged)
    return case, main, pr, head


def install_calls(commands):
    return [row for row in commands.calls("uv") if row["argv"][:2] == ["tool", "install"]]


def test_self_merge_reinstalls_merged_source_and_checks_installed_copy_without_touching_checkout(
    git_history, tmp_path, monkeypatch, capsys,
):
    from conftest import git_command

    commands = fake_commands(tmp_path, monkeypatch)
    case, main, pr, head = reinstall_case(git_history, tmp_path, monkeypatch)
    before = git_command(case.root, "rev-parse", "HEAD")
    local = case.root / "unrelated-local.txt"
    local.write_text("keep me\n")
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) == 0
    assert case.tracker.merged_prs == [pr]
    assert git_command(case.history.origin, "rev-parse", "refs/heads/main") == head
    assert git_command(case.root, "rev-parse", "HEAD") == before
    assert local.read_text() == "keep me\n"
    calls = install_calls(commands)
    assert len(calls) == 1, "self merge must reinstall Foragent exactly once"
    call = calls[0]
    assert call["argv"][:-1] in (["tool", "install", "--force"],
                                 ["tool", "install", "--force", "--reinstall"])
    source = Path(call["argv"][-1])
    if not source.is_absolute():
        source = Path(call["cwd"]) / source
    assert source.resolve() == Path(call["source_root"])
    assert source.resolve() != case.root.resolve()
    # Captured by the executable at install time; a temporary source may be cleaned up.
    assert call["source_init"] == PR_VERSION
    installed = commands.tool_dir / "foragent/lib/python3.12/site-packages/foragent/__init__.py"
    assert installed.read_text() == PR_VERSION
    assert any(row["argv"] in (["tool", "dir"], ["tool", "dir", "--bin"])
               for row in commands.calls("uv"))
    output = capsys.readouterr().out.lower()
    assert any("reinstalled" in line and "matches" in line for line in output.splitlines())


@pytest.mark.parametrize("mode", ["mismatch", "error"])
def test_reinstall_failure_is_reported_and_recorded_after_the_merge_stays_successful(
    git_history, tmp_path, monkeypatch, capsys, mode,
):
    commands = fake_commands(tmp_path, monkeypatch)
    monkeypatch.setenv("FA_D57_INSTALL_MODE", mode)
    case, main, pr, head = reinstall_case(git_history, tmp_path, monkeypatch)
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) == 0
    assert case.tracker.merged_prs == [pr], "an installer failure must not undo the merge"
    assert len(install_calls(commands)) == 1, "self merge must attempt reinstall before reporting failure"
    captured = capsys.readouterr()
    assert "reinstall failed" in (captured.out + captured.err).lower()
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "reinstall-failed" and event["summary"] for event in events)


@pytest.mark.parametrize("project_name", ["another-project", None], ids=["other-name", "no-pyproject"])
def test_other_repositories_do_not_call_uv_but_a_subsequent_self_merge_does(
    git_history, tmp_path, monkeypatch, project_name,
):
    commands = fake_commands(tmp_path, monkeypatch)
    case, main, pr, head = reinstall_case(git_history, tmp_path, monkeypatch, project_name=project_name)
    assert main(["orch", "merge", str(pr), "--approved-by", "jin"]) == 0
    assert case.tracker.merged_prs == [pr]
    assert commands.calls("uv") == [], "a src/foragent directory alone does not identify the project"
    # Positive control: the next PR changes the same repo's project identity.
    next_pr = case.tracker.create_pr(head="foragent/issue-2", base="main", title="Self project", body="Review")
    update_pr_tree(case, next_pr, head, {"pyproject.toml": '[project]\nname = "foragent"\n'})
    case.tracker.set_pr_checks(next_pr, "pass")
    assert main(["orch", "merge", str(next_pr), "--approved-by", "jin"]) == 0
    assert case.tracker.merged_prs == [pr, next_pr]
    assert len(install_calls(commands)) == 1, "the foragent project name must enable reinstall"
