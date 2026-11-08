"""The acceptance writer receives the project's configured test command (n91).

Drives `fa order prepare` and `fa order publish --dry-run` through the CLI with the
offline lane fixtures: the writer and reviewer are fake executors, the tracker and git
remote are local fakes, and the uv runner is a stub on an isolated PATH. No model,
GitHub write, commit, push or signal is used.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

import pytest
import yaml

from foragent import cli, executors
from foragent.executors import ExecResult
from lane_tests_case import (  # noqa: F401
    STATEMENT, assert_no_publication, disk_plan, lane_case, offline_publication, prepare, publish)


TEST_PATH = "tests/acceptance/test_notice_runner.py"
CANDIDATE = 'def test_notice_is_delivered_once():\n    assert False, "notice delivery is missing"\n'
FALLBACK = f"python -m pytest -q {TEST_PATH}"


def _configure(case, monkeypatch, test_command):
    """Give the project a test_command (or none) in both the loaded config and .foragent.yml."""
    case.config = replace(case.config, test_command=test_command)
    monkeypatch.setattr(cli, "load_config", lambda root: case.config)
    data = yaml.safe_load((case.root / ".foragent.yml").read_text())
    if test_command is None:
        data.pop("test_command", None)
    else:
        data["test_command"] = test_command
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(data))


def _writer_prompt(case):
    writers = [call for call in case.calls if call["role"] == "writer"]
    assert writers, f"no writer request reached the executor: {[c['role'] for c in case.calls]}"
    tree = Path(writers[0]["worktree"])
    prompt = writers[0]["prompt"]
    # Temporary paths carry pytest's test names; mask them so only the request's own words count.
    for path, mark in ((tree, "<SCRATCH-TREE>"), (case.root, "<CHECKOUT>")):
        for spelling in {str(path), str(path.resolve())}:
            prompt = prompt.replace(spelling, mark)
    return prompt, tree


def _paragraphs(prompt):
    return [block for block in re.split(r"\n\s*\n", prompt) if block.strip()]


def _test_command_context(prompt):
    """Paragraphs of the writer request that speak about the project's test command setting."""
    return [block for block in _paragraphs(prompt)
            if re.search(r"test[_ ]command", block, re.IGNORECASE)]


# Statement 1 ---------------------------------------------------------------

@pytest.mark.parametrize("configured", ["uv run pytest -q", "hatch run test:pytest -x --maxfail=1"])
def test_writer_request_carries_the_configured_test_command_and_the_scratch_location(
        lane_case, capsys, monkeypatch, configured):
    case = lane_case
    _configure(case, monkeypatch, configured)
    prepare(case, capsys)
    prompt, tree = _writer_prompt(case)

    assert configured in prompt, (
        f"The writer request does not contain the configured test_command {configured!r}:\n{prompt}")
    holding = [block for block in _paragraphs(prompt) if configured in block]
    # The exact command, not a reshaped one: it appears verbatim as one token sequence.
    assert any(re.search(rf"(^|[\s`'\"]){re.escape(configured)}($|[\s`'\".,;:])", block, re.M)
               for block in holding), holding
    assert any(re.search(r"test_command|configur", block, re.IGNORECASE) for block in holding), (
        f"The command is not identified as the project's configuration: {holding}")
    assert any(re.search(r"scratch|working (directory|copy)", block, re.IGNORECASE)
               or "<SCRATCH-TREE>" in block for block in holding), (
        f"The command's context does not say it runs in the scratch tree: {holding}")
    # The other runner never leaks in as if it were configured.
    other = {"uv run pytest -q", "hatch run test:pytest -x --maxfail=1"} - {configured}
    assert not any(command in prompt for command in other), prompt
    assert_no_publication(case)


# Statement 3 ---------------------------------------------------------------

def test_unset_test_command_is_reported_and_repository_tooling_stays_available(
        lane_case, capsys, monkeypatch):
    case = lane_case
    _configure(case, monkeypatch, None)
    prepare(case, capsys)
    prompt, _ = _writer_prompt(case)

    context = _test_command_context(prompt)
    assert context, f"The writer request does not mention the test_command setting at all:\n{prompt}"
    assert any(re.search(r"not configured|not set|unset|no test[_ ]command|is none|\(none\)|null",
                         block, re.IGNORECASE) for block in context), (
        f"The writer is not told that no test_command is configured: {context}")
    # No command is presented as if it were the configured one.
    for block in context:
        assert "uv run pytest" not in block and "hatch run" not in block, block
    # Repository-based tooling discovery remains permitted.
    assert re.search(r"tooling from the repository|repository'?s? (own )?(test )?tool", prompt,
                     re.IGNORECASE), prompt
    assert_no_publication(case)


# Statement 2 ---------------------------------------------------------------

def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def uv_only_host(lane_case, monkeypatch, tmp_path_factory):
    """A uv project whose PATH offers uv and git but no bare python or python3."""
    case = lane_case
    tools = tmp_path_factory.mktemp("uv-only-tools")
    # `uv run pytest ...` runs this interpreter's pytest; anything else from uv is refused.
    _stub(tools / "uv", "#!/bin/sh\n"
          'if [ "$1" = run ] && [ "$2" = pytest ]; then shift 2; '
          f'PYTHONDONTWRITEBYTECODE=1 exec "{sys.executable}" -m pytest -p no:cacheprovider "$@"; fi\n'
          'echo "uv stub: unsupported $*" >&2\nexit 97\n')
    git = shutil.which("git")
    assert git, "git is needed for the offline publication fixture"
    os.symlink(git, tools / "git")
    _stub(case.root / ".venv/bin/pytest", "#!/bin/sh\nexit 97\n")
    pyproject = ('[project]\nname = "demo"\nversion = "0"\n'
                 '[dependency-groups]\ndev = ["pytest"]\n')
    (case.root / "pyproject.toml").write_text(pyproject)
    (case.root / "uv.lock").write_text("version = 1\n")
    monkeypatch.setenv("PATH", str(tools))
    assert shutil.which("python") is None and shutil.which("python3") is None
    modes = {path: stat.S_IMODE(path.stat().st_mode)
             for path in [tools / "uv", case.root / ".venv/bin/pytest"]}
    return case, tools, modes


def _agent_fakes(case, monkeypatch):
    """A writer that runs the configured command when it is given, else guesses bare python."""

    def run(role, **kw):
        case.calls.append({"role": role, **kw})
        tree = Path(kw["worktree"])
        prompt = kw["prompt"]
        if role == "writer":
            path = tree / TEST_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CANDIDATE)
            configured = case.config.test_command
            command = f"{configured} {TEST_PATH}" if configured and configured in prompt else FALLBACK
            case.written_done_when = [command]
            message = {"acceptance_files": {TEST_PATH: CANDIDATE}, "done_when": [command],
                       "questions": [], "notes": []}
        else:
            written = case.written_done_when
            message = {"verdict": "pass", "reason": "Measured red by assertion.", "questions": [],
                       "coverage": [{"statement": STATEMENT, "tests": [TEST_PATH],
                                     "break_it": ["wrong value", "skipped effect", "doubled effect"]}],
                       "red": [{"command": command, "collected": 1, "failed": 1, "exit_code": 1,
                                "output": "AssertionError: notice delivery is missing",
                                "failure_kind": "assertion"} for command in dict.fromkeys(written)]}
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), log, session_id=f"{role}-{len(case.calls)}")

    monkeypatch.setattr(executors.CodexExecutor, "run", lambda self, **kw: run("writer", **kw))
    monkeypatch.setattr(executors.ClaudeExecutor, "run", lambda self, **kw: run("reviewer", **kw))


def test_supplied_command_yields_done_when_that_passes_publication_preflight(uv_only_host, capsys, monkeypatch):
    case, tools, modes = uv_only_host
    _configure(case, monkeypatch, "uv run pytest -q")
    _agent_fakes(case, monkeypatch)
    project = {name: (case.root / name).read_bytes() for name in ("pyproject.toml", "uv.lock", ".foragent.yml")}

    capsys.readouterr()
    row = prepare(case, capsys)
    assert row["status"] == "reviewed", json.dumps(row, indent=1)
    assert len([c for c in case.calls if c["role"] == "writer"]) == 1
    draft = disk_plan(case)
    issue = draft["issues"][0]
    done_when = issue["done_when"]

    # Publish the prepared Done when commands as a draft through the executable preflight.
    issue.pop("acceptance_files", None)
    case.plan = draft
    case.save()
    code, out, err = publish(case, capsys, "--dry-run")
    assert code == 0, f"Prepared Done when commands failed publication's executable preflight: {out}{err}"
    assert "missing executable" not in out + err
    assert done_when == [f"uv run pytest -q {TEST_PATH}"], done_when

    # Neither the project's runner nor executable permissions were changed.
    assert {name: (case.root / name).read_bytes() for name in project} == project
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes} == modes
    assert sorted(p.name for p in tools.iterdir()) == ["git", "uv"]
    assert_no_publication(case)

