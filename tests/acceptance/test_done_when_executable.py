"""Acceptance test for done-when-executable. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
import shutil
from types import SimpleNamespace

import pytest

from test_order import plan, preread_done, publishing
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home


CHECK = "tests/acceptance/test_login.py"
MISSING = f"python -m pytest -q {CHECK}"
RUNNABLE = f"uv run pytest -q {CHECK}"


@pytest.fixture
def executable_case(publishing, plan, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", skill_libraries=[])
    main = publish_cli((root, config, tracker, git, events), monkeypatch)
    monkeypatch.chdir(root)
    # A repository that uses uv, with python3 but no python on the check PATH.
    (root / "pyproject.toml").write_text(
        '[project]\nname = "offline-checks"\nversion = "0.0.0"\n'
        '[dependency-groups]\ndev = ["pytest"]\n', encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name in ("uv", "python3"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("python") is None
    assert shutil.which("uv") == str(bin_dir / "uv")
    assert shutil.which("python3") == str(bin_dir / "python3")
    plan["issues"] = [plan["issues"][0]]
    return SimpleNamespace(root=root, plan=plan, main=main, tracker=tracker,
                           events=events, bin_dir=bin_dir)


def invoke(case, capsys, commands, *options):
    case.plan["issues"][0]["done_when"] = commands
    path = case.root / "PLAN.json"
    path.write_text(json.dumps(case.plan), encoding="utf-8")
    before = {p.relative_to(case.root): p.read_bytes()
              for p in case.root.rglob("*") if p.is_file()}
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "publish", str(path),
                               "--repo-root", str(case.root), *options])
    captured = capsys.readouterr()
    after = {p.relative_to(case.root): p.read_bytes()
             for p in case.root.rglob("*") if p.is_file()}
    return code, captured.out + captured.err, before, after


def assert_no_publication(case, before, after):
    assert after == before, "Publication changed files"
    assert case.tracker.issues == {}, "Publication created issues"
    assert case.tracker.labels == {}, "Publication wrote labels"
    assert case.events == [], "Publication committed, pushed, or created an issue"


@pytest.mark.parametrize("commands,missing", [
    ([MISSING], ["python"]),
    ([MISSING, f"unavailable-check-runner --verify {CHECK}"],
     ["python", "unavailable-check-runner"]),
    ([MISSING, f"python -m pytest --collect-only -q {CHECK}"], ["python", "python"]),
])
def test_dry_run_reports_every_missing_executable(executable_case, capsys, commands, missing):
    case = executable_case
    code, message, before, after = invoke(case, capsys, commands, "--dry-run")
    assert code != 0, f"Dry-run accepted missing executables {missing}: {message}"
    lines = [line for line in message.splitlines() if line.strip()]
    assert len(lines) == len(missing), f"Expected one diagnostic per missing command: {message}"
    for name in set(missing):
        diagnostics = [line for line in lines if name in line]
        assert len(diagnostics) == missing.count(name), message
        suggestion = "uv run pytest" if name == "python" else f"uv run {name}"
        assert all(suggestion in line for line in diagnostics), message
    assert_no_publication(case, before, after)


@pytest.mark.parametrize("executable,command", [
    ("uv", RUNNABLE),
    ("python3", f"python3 -m pytest -q {CHECK}"),
    ("python", MISSING),
])
def test_dry_run_accepts_resolving_executables(executable_case, capsys, executable, command):
    case = executable_case
    if executable == "python":
        # The spelling is valid when it exists; this must not become a python ban.
        shutil.copy2(case.bin_dir / "python3", case.bin_dir / "python")
    code, message, before, after = invoke(case, capsys, [command], "--dry-run")
    assert code == 0, message
    assert case.plan["issues"][0]["title"] in message, message
    assert_no_publication(case, before, after)


def test_dry_run_mixed_plan_reports_only_the_missing_command(executable_case, capsys):
    case = executable_case
    # Put the runnable command first so checking only the first command fails.
    code, message, before, after = invoke(case, capsys, [RUNNABLE, MISSING], "--dry-run")
    assert code != 0, f"Dry-run accepted a mixed plan with missing python: {message}"
    lines = [line for line in message.splitlines() if line.strip()]
    assert len(lines) == 1, f"Only the missing command needs a diagnostic: {message}"
    assert "python" in lines[0] and "uv run pytest" in lines[0], message
    assert_no_publication(case, before, after)


def test_approved_publish_refuses_missing_executable_without_writes(executable_case, capsys):
    case = executable_case
    # The existing clean-preread fake keeps this refusal about executability.
    code, message, before, after = invoke(case, capsys, [MISSING], "--approve")
    assert code != 0, f"Approved publish accepted missing python: {message}"
    lines = [line for line in message.splitlines() if line.strip()]
    assert len(lines) == 1, message
    assert "python" in lines[0] and "uv run pytest" in lines[0], message
    assert_no_publication(case, before, after)
