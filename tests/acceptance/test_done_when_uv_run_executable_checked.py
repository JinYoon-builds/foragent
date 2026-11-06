"""Done when commands run through `uv run` are checked for the executable after `uv run`.

Locked by Foragent; do not edit. Drives `fa order publish --dry-run` only, with
the order publication fixtures and a temporary uv project. Nothing is run: the
fake tools exit 97 if anything ever executes them.
"""

from dataclasses import replace
import json

import pytest

from test_order import plan, publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


VENV_TOOL = "fa-venv-check"
PATH_TOOL = "fa-path-check"
TYPO = "pytestx"


def _tool(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 97\n")
    path.chmod(0o755)


@pytest.fixture
def uv_project(publishing, plan, monkeypatch, tmp_path_factory):  # noqa: F811
    """A uv project whose .venv holds VENV_TOOL; PATH holds only uv and PATH_TOOL."""
    root, config, tracker, git, events = publishing
    tools = tmp_path_factory.mktemp("tools")
    _tool(tools / "uv")
    _tool(tools / PATH_TOOL)
    _tool(root / ".venv/bin" / VENV_TOOL)
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n'
                                         '[dependency-groups]\ndev = ["pytest"]\n')
    (root / "uv.lock").write_text("version = 1\n")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.chdir(root)
    plan["issues"] = [plan["issues"][0]]

    def publish(done_when, language):
        localized = replace(config, language=language, skill_libraries=[])
        main = publish_cli((root, localized, tracker, git, events), monkeypatch)
        plan["issues"][0]["done_when"] = list(done_when)
        path = root / "PLAN.json"
        path.write_text(json.dumps(plan))
        return cli_exit(main, ["order", "publish", str(path), "--repo-root", str(root), "--dry-run"])

    return publish, tracker, events


def _refusal(capsys):
    captured = capsys.readouterr()
    return captured.out + captured.err


@pytest.mark.parametrize("language", ["en", "ko"])
def test_uv_run_executable_missing_everywhere_is_refused_by_name(uv_project, capsys, language):
    publish, tracker, events = uv_project
    # Unchanged: plain uv commands, PATH executables, shell built-ins and uv run of
    # a .venv or PATH executable all pass.
    accepted = ["uv sync", f"{PATH_TOOL} --quick", "true", "cd .",
                f"uv run {VENV_TOOL} -q", f"uv run {PATH_TOOL}"]
    capsys.readouterr()
    assert publish(accepted, language) == 0, _refusal(capsys)
    capsys.readouterr()
    code = publish([f"uv run {TYPO} -q"], language)
    output = _refusal(capsys)
    assert code != 0, f"uv run of an executable that exists nowhere was accepted: {output}"
    assert TYPO in output, output
    assert "uv run uv run" not in output, output
    assert tracker.issues == {} and events == []


@pytest.mark.parametrize("language", ["en", "ko"])
def test_missing_executable_absent_from_uv_env_gets_no_uv_run_suggestion(uv_project, capsys, language):
    publish, tracker, events = uv_project
    capsys.readouterr()
    code = publish([f"{TYPO} -q"], language)
    output = _refusal(capsys)
    assert code != 0, output
    assert TYPO in output, output
    assert "uv run" not in output, f"Suggested uv run for an executable uv cannot find: {output}"
    assert tracker.issues == {} and events == []


@pytest.mark.parametrize("language", ["en", "ko"])
def test_venv_only_executable_is_suggested_per_line_and_accepted_through_uv_run(uv_project, capsys, language):
    publish, tracker, events = uv_project
    capsys.readouterr()
    code = publish([f"{VENV_TOOL} --quick", f"{TYPO} -q"], language)
    output = _refusal(capsys)
    assert code != 0, output
    venv_lines = [line for line in output.splitlines() if VENV_TOOL in line]
    typo_lines = [line for line in output.splitlines() if TYPO in line]
    assert len(venv_lines) == 1 and len(typo_lines) == 1, output
    assert f"uv run {VENV_TOOL} --quick" in venv_lines[0], output
    assert "uv run" not in typo_lines[0], f"Suggested uv run for {TYPO}: {output}"
    capsys.readouterr()
    assert publish([f"uv run {VENV_TOOL} --quick"], language) == 0, _refusal(capsys)
    assert tracker.issues == {} and events == []
