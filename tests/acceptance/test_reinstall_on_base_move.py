"""Acceptance test for reinstall-on-base-move: the gate follows the base, whoever merged.

Today the gate reinstalls its own tool only after a merge it made itself. When a
person merges a PR, the gate keeps running the old build. Each tick must compare
the base branch with the installed tool's revision and, when the new commits
touch the tool's code (src/ or pyproject.toml), reinstall it the same way it
does after its own merge. A move that only touches agent memory or docs does not
reinstall, and a second tick on the same revision does not reinstall again.

Real gate tick, real Git history, real install and publish code. Only the
`uv` command is fake: it records which source tree it was asked to install and
lays out the installed files the way uv does. No real install runs.
"""

from pathlib import Path
import shutil
import subprocess

import pytest

from foragent import capacity, launchd, scheduler, tool_install
from test_orch_review_handoff import make_case, tick_context


@pytest.fixture
def gate(git_history, tmp_path, monkeypatch):
    home = tmp_path / "home"
    machine = home / ".local/share/foragent"
    machine.mkdir(parents=True)
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    monkeypatch.setattr(launchd, "installed_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(tool_install.psutil, "process_iter", lambda *args, **kwargs: iter(()))

    git_history.update({
        "pyproject.toml": '[project]\nname = "foragent"\n',
        "src/foragent/__init__.py": "BUILD = 'first'\n",
    }, message="Foragent source")
    case = make_case(git_history, tmp_path / "repo", monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    case.tracker.remove_labels(1, ["running"])
    case.history = git_history
    case.installs = []  # The BUILD of each source tree uv was asked to install, in order.
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if not argv or argv[0] != "uv":
            return real_run(argv, *args, **kwargs)
        if argv[1:] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(bin_dir), stderr="")
        assert argv[1:3] == ["tool", "install"], argv
        source = Path(argv[-1]) / "src/foragent"
        build = (source / "__init__.py").read_text().strip()
        case.installs.append(build)
        tools = Path(kwargs["env"]["UV_TOOL_DIR"])
        package = tools / "foragent/lib/python3.12/site-packages/foragent"
        shutil.copytree(source, package)
        (tools / "foragent/bin").mkdir(parents=True, exist_ok=True)
        entry = Path(kwargs["env"]["UV_TOOL_BIN_DIR"])
        entry.mkdir(parents=True, exist_ok=True)
        for name in tool_install.ENTRY_POINTS:
            script = tools / "foragent/bin" / name
            script.write_text(f"#!/bin/sh\necho {build!r}\n")
            script.chmod(0o755)
            (entry / name).symlink_to(script)
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    # The machine already runs the build of the current base, installed the normal way.
    first = tmp_path / "first-source"
    first.mkdir()
    archive = subprocess.run(["git", "-C", str(git_history.origin), "archive", git_history.tip],
                             capture_output=True, check=True).stdout
    real_run(["tar", "-x", "-C", str(first)], input=archive, check=True)
    tool_install.install(first, cwd=first, revision=git_history.tip)
    assert case.installs == ["BUILD = 'first'"]
    case.installs.clear()
    # The gate is current: a tick on the unchanged base installs nothing.
    tick(case)
    assert case.installs == [], "A tick on the installed revision must not reinstall"
    return case


def tick(case):
    report = scheduler.tick(tick_context(case, start_runs=False))
    assert report.skipped is None, report
    return report


def person_merges(case, files, message="Merged by a person"):
    """Someone else (a person running gh pr merge) moves the base; the gate did not merge it."""
    case.history.update(files, message=message)


def test_a_base_move_by_someone_else_that_touches_src_reinstalls_once(gate):
    case = gate
    person_merges(case, {"src/foragent/__init__.py": "BUILD = 'second'\n"})

    tick(case)
    assert case.installs == ["BUILD = 'second'"], (
        f"The next tick must reinstall the moved base's source exactly once: {case.installs}"
    )
    tick(case)
    tick(case)
    assert case.installs == ["BUILD = 'second'"], (
        f"Later ticks on the same revision must not reinstall again: {case.installs}"
    )


def test_a_move_that_touches_pyproject_reinstalls(gate):
    case = gate
    person_merges(case, {"pyproject.toml": '[project]\nname = "foragent"\ndependencies = ["pyyaml"]\n'})

    tick(case)
    assert case.installs == ["BUILD = 'first'"], (
        f"A dependency change is a change to the tool: reinstall once: {case.installs}"
    )


def test_a_move_of_only_agent_memory_or_docs_does_not_reinstall(gate):
    case = gate
    person_merges(case, {".claude/agent-memory/fa-orch/MEMORY.new.md": "- [id:x] a lesson\n"},
                  message="Lane memory")
    person_merges(case, {"docs/decisions.md": "# Decisions\n"}, message="Docs")

    tick(case)
    tick(case)
    assert case.installs == [], (
        f"Memory and docs are not the tool: the gate must keep its build without reinstalling: {case.installs}"
    )

    # A later source move still counts every commit since the installed revision.
    person_merges(case, {"src/foragent/__init__.py": "BUILD = 'third'\n"})
    tick(case)
    assert case.installs == ["BUILD = 'third'"], case.installs
