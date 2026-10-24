"""Regression coverage for adoption reports and CI source preservation."""

import json
import os
import shlex
import sys
import time

import pytest

from foragent import adopt, cli


def workflow(root, script, *, filename="ci.yml"):
    path = root / ".github/workflows" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# keep this comment\r\njobs:\r\n  test:\r\n    steps:\r\n" + script)
    return path


@pytest.mark.parametrize("line", [
    b"      - run: uv run pytest -q  # keep YAML comment\r\n",
    b"      - run: 'python -m pytest -q' # keep quote\r\n",
    b'      - run: "pytest -k \\"hello\\"" # keep escape\r\n',
    b'      - run: "pytest -k \'#tag\' # shell comment"\r\n',
    b"      - run: pytest -q",
    "      - run: pytest -k 한글\r\n".encode(),
])
def test_ci_preserves_every_other_byte(tmp_path, line):
    path = workflow(tmp_path, line)
    before = path.read_bytes()
    assert adopt.ci(tmp_path)["needs_change"] == [".github/workflows/ci.yml"]
    assert path.read_bytes() == before
    assert adopt.ci(tmp_path, True)["changed"] == [".github/workflows/ci.yml"]
    after = path.read_bytes()
    assert after.count(b" --ignore=tests/acceptance") == 1
    assert after.replace(b" --ignore=tests/acceptance", b"") == before
    assert adopt._ci(tmp_path, "tests/acceptance") == [
        {"file": ".github/workflows/ci.yml", "runs_acceptance": False},
    ]
    assert adopt.ci(tmp_path, True) == {
        "needs_change": [], "changed": [], "manual": [],
        "collect_only": [{"file": ".github/workflows/ci.yml",
                          "add": ("uv run pytest" if b"uv run" in line else
                                  "python -m pytest" if b"python -m" in line else "pytest")
                          + " --collect-only -q"}],
    }
    assert path.read_bytes() == after


def test_ci_checks_each_pytest_command_and_keeps_other_runners(tmp_path):
    path = workflow(tmp_path, (
        b"      - run: pytest --ignore=tests/acceptance\r\n"
        b"      - run: echo pytest\r\n"
        b"      - run: npm test\r\n"
        b"      - run: pnpm test --filter app\r\n"
        b"      - run: go test ./...\r\n"
        b"      - run: pytest -q --ignore=elsewhere\r\n"
        b"      - run: pytest --ignore=tests/acceptance-more\r\n"
        b"      - run: pytest -k 'a;b' && pytest tests/unit\r\n"
        b"      - run: pytest 2>&1 | tee results.log\r\n"
    ))
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["manual"] == [
        {"file": ".github/workflows/ci.yml", "line": line}
        for line in ("npm test", "pnpm test --filter app", "go test ./...")
    ]
    assert report["needs_change"] == report["changed"] == [".github/workflows/ci.yml"]
    assert path.read_bytes() == before.replace(
        b"pytest -q --ignore=elsewhere", b"pytest -q --ignore=elsewhere --ignore=tests/acceptance",
    ).replace(
        b"pytest --ignore=tests/acceptance-more", b"pytest --ignore=tests/acceptance-more --ignore=tests/acceptance",
    ).replace(
        b"pytest -k 'a;b' && pytest tests/unit",
        b"pytest -k 'a;b' --ignore=tests/acceptance && pytest tests/unit --ignore=tests/acceptance",
    ).replace(
        b"pytest 2>&1 | tee results.log", b"pytest 2>&1 --ignore=tests/acceptance | tee results.log",
    )
    assert adopt.ci(tmp_path, True)["needs_change"] == []


@pytest.mark.parametrize("style", ["|", ">-"])
def test_ci_reports_multiline_pytest_without_editing(tmp_path, style):
    path = workflow(tmp_path, f"      - run: {style}\r\n          pytest -q\r\n".encode())
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["needs_change"] == [".github/workflows/ci.yml"]
    assert report["changed"] == []
    assert report["manual"][0]["line"].strip() == "pytest -q"
    assert path.read_bytes() == before


def test_ci_empty_and_non_workflows_are_ignored(tmp_path):
    assert adopt.ci(tmp_path, True) == {"needs_change": [], "changed": [], "manual": [], "collect_only": []}
    workflow(tmp_path, b"      - run: pytest\n", filename="ci.txt")
    assert adopt.ci(tmp_path, True) == {"needs_change": [], "changed": [], "manual": [], "collect_only": []}


def test_ci_invalid_yaml_returns_error_before_writing(tmp_path, capsys):
    path = workflow(tmp_path, b"      - run: [\n")
    before = path.read_bytes()
    assert cli.main(["adopt", "ci", "--repo-root", str(tmp_path), "--json"]) == 1
    assert "cannot read workflow" in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_ci_custom_path_with_spaces(tmp_path, quote):
    (tmp_path / ".foragent.yml").write_text("acceptance_dir: 'checks/locked tests/'\n")
    path = workflow(tmp_path, f"      - run: {quote}pytest -q{quote}\r\n".encode())
    before = path.read_bytes()
    assert adopt.ci(tmp_path, True)["changed"] == [".github/workflows/ci.yml"]
    assert adopt._ci(tmp_path, "checks/locked tests")[0]["runs_acceptance"] is False
    assert adopt.ci(tmp_path, True)["changed"] == []
    assert path.read_bytes().startswith(before.split(b"pytest")[0])


def test_shared_detection_preserves_quoted_hash_and_resumes_after_comments():
    assert adopt._test_invocations("pytest -k '#tag' --ignore=tests/acceptance # comment\nnpm test") == [
        ["pytest", "-k", "#tag", "--ignore=tests/acceptance"], ["npm", "test"],
    ]


@pytest.mark.parametrize("prefix", [
    "pytest", "uv run pytest", "uv run --no-sync pytest", "python -m pytest",
    "poetry run python3 -m pytest", "'/path with spaces/pytest'",
])
def test_ci_collection_preserves_prefix_and_reports_once(tmp_path, capsys, prefix):
    path = workflow(tmp_path, (
        f"      - run: |\n          {prefix} -q --ignore=tests/acceptance\n"
        "          pytest tests/unit\n          echo --collect-only\n"
    ).encode())
    workflow(tmp_path, b"      - run: npm test\n", filename="node.yml")
    before = path.read_bytes()
    expected = {"file": ".github/workflows/ci.yml", "add": prefix + " --collect-only -q"}
    assert adopt.ci(tmp_path)["collect_only"] == [expected]
    assert cli.main(["adopt", "ci", "--repo-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert expected["file"] in output and expected["add"] in output
    assert path.read_bytes() == before


def test_ci_collection_detection_is_per_workflow_and_exclusion_keeps_collection(tmp_path):
    path = workflow(tmp_path, (
        b"      - run: pytest --collect-only -q\n"
        b"      - run: uv run pytest -q\n"
    ))
    workflow(tmp_path, b"      - run: python -m pytest -q\n", filename="other.yaml")
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["collect_only"] == [
        {"file": ".github/workflows/other.yaml", "add": "python -m pytest --collect-only -q"},
    ]
    assert path.read_bytes() == before.replace(
        b"uv run pytest -q", b"uv run pytest -q --ignore=tests/acceptance",
    )
    assert adopt.ci(tmp_path, True)["changed"] == []


def test_ci_yaml_merges_and_anchors_only_insert_once(tmp_path):
    path = workflow(tmp_path, (
        b"      - &tests\r\n"
        b"        run: &command pytest -q\r\n"
        b"      - <<: *tests\r\n"
        b"      - run: *command\r\n"
    ))
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["changed"] == [".github/workflows/ci.yml"]
    assert path.read_bytes() == before.replace(b"pytest -q", b"pytest -q --ignore=tests/acceptance")
    assert adopt.ci(tmp_path, True)["changed"] == []


@pytest.mark.parametrize(("files", "expected"), [
    ({"pyproject.toml": "", "go.mod": ""}, "uv run pytest"),
    ({"requirements.txt": "pytest\n"}, "uv run pytest"),
    ({"package.json": '{"scripts":{"test":"vitest"}}'}, "npm test"),
    ({"package.json": '{"scripts":{"test":"vitest"}}', "pnpm-lock.yaml": ""}, "pnpm test"),
    ({"package.json": '{"scripts":{"test":"vitest"},"packageManager":"pnpm@10"}'}, "pnpm test"),
    ({"go.mod": "module example\n"}, "go test ./..."),
    ({"package.json": "broken", "go.mod": ""}, "go test ./..."),
    ({"package.json": "[]"}, None),
    ({"package.json": '{"scripts":{"build":"vite build"}}'}, None),
])
def test_baseline_command_detection(tmp_path, files, expected):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    assert adopt._detect_test_command(tmp_path) == expected


def test_baseline_override_runs_once_in_root_without_parsing_non_pytest(tmp_path, capsys):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "runner.py").write_text(
        "from pathlib import Path\n"
        "with Path('runs').open('a') as f: f.write('once\\n')\n"
        "print('FAILED tests/test_example.py::test_example - error')\n"
        "raise SystemExit(3)\n"
    )
    command = shlex.join([sys.executable, "runner.py"])
    assert cli.main(["adopt", "baseline", "--repo-root", str(tmp_path),
                     "--command", command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": command, "exit_code": 3, "passed": False, "failing": [],
    }
    assert (tmp_path / "runs").read_text() == "once\n"


def test_baseline_pytest_reports_colored_parameterized_ids_and_quotes_ignore(tmp_path):
    (tmp_path / ".foragent.yml").write_text("acceptance_dir: 'checks/locked tests/'\n")
    runner = tmp_path / "pytest"
    runner.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > args\n"
        "printf '\\033[31mFAILED tests/test_example.py::test_case[a b] - AssertionError\\033[0m\\n'\n"
        "exit 1\n"
    )
    runner.chmod(0o755)
    report = adopt.baseline(tmp_path, shlex.quote(str(runner)))
    assert report["passed"] is False
    assert report["failing"] == ["tests/test_example.py::test_case[a b]"]
    assert (tmp_path / "args").read_text().splitlines() == ["--ignore=checks/locked tests", "-rf"]


def test_baseline_timeout_stops_shell_and_runner(tmp_path, capsys, monkeypatch, fake_process_table):
    import subprocess

    class Process:
        pid = 42000
        returncode = None
        calls = 0

        def __init__(self, *args, **kwargs):
            fake_process_table.add(self.pid, cwd=tmp_path, start_time=10)
            fake_process_table.add(42001, ppid=self.pid, pgid=self.pid, sid=self.pid,
                                   cwd=tmp_path, start_time=11)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert not {42000, 42001} & fake_process_table.live

        def communicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("offline baseline", 1)
            return b"", None

    monkeypatch.setattr(adopt.subprocess, "Popen", Process)
    assert cli.main(["adopt", "baseline", "--repo-root", str(tmp_path),
                     "--command", "offline-baseline", "--timeout", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == 124
    assert report["passed"] is False
    assert report["failing"] == []
    assert not {42000, 42001} & fake_process_table.live
    assert all(kind == "pid" and sig != 0 for kind, _, sig in fake_process_table.calls)
