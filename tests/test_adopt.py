"""Baseline runner selection and execution beyond the fixed acceptance examples."""

import json
import shlex
import sys

import pytest

from foragent import adopt, cli


@pytest.mark.parametrize("files, expected", [
    ({"pyproject.toml": "", "package.json": '{"scripts":{"test":"test"}}', "go.mod": ""}, "uv run pytest"),
    ({"requirements.txt": "pytest"}, "uv run pytest"),
    ({"requirements-dev.txt": "pytest"}, "uv run pytest"),
    ({"package.json": '{"scripts":{"test":"test"}}', "go.mod": ""}, "npm test"),
    ({"package.json": '{"scripts":{"test":"test"}}', "pnpm-lock.yaml": ""}, "pnpm test"),
    ({"package.json": '{"scripts":{"test":"test"},"packageManager":"pnpm@10"}'}, "pnpm test"),
    ({"package.json": '{"scripts":{"build":"build"}}', "go.mod": ""}, "go test ./..."),
    ({"package.json": "invalid", "go.mod": ""}, "go test ./..."),
    ({"package.json": "[]"}, None),
])
def test_detected_command_runs_once(tmp_path, monkeypatch, files, expected):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    calls = []

    def run(argv, *, cwd, timeout_s, log_path):
        calls.append((argv, cwd, timeout_s))
        log_path.write_text("")
        return 0, False, ""

    monkeypatch.setattr(adopt, "run_logged", run)
    report = adopt.baseline(tmp_path, timeout=23)
    if expected is None:
        assert report == {"command": None, "passed": None, "skipped": "no test command found"}
        assert calls == []
    else:
        command = expected + (" --ignore=tests/acceptance -rf" if expected.endswith("pytest") else "")
        assert calls == [(["/bin/sh", "-c", command], tmp_path.resolve(), 23)]
        assert report == {"command": command, "exit_code": 0, "passed": True, "failing": []}


def test_explicit_non_pytest_runs_in_root_once_and_does_not_parse_failures(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "path = Path('runs.txt')\n"
        "path.write_text(path.read_text() + 'run\\n' if path.exists() else 'run\\n')\n"
        "print('FAILED tests/test_fake.py::test_fake - message')\n"
        "raise SystemExit(3)\n"
    )
    command = f"{shlex.quote(sys.executable)} check.py"
    assert cli.main(["adopt", "baseline", "--repo-root", str(tmp_path), "--command", command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": command, "exit_code": 3, "passed": False, "failing": [],
    }
    assert (tmp_path / "runs.txt").read_text() == "run\n"


def test_timeout_returns_json_and_stops_child(tmp_path, capsys):
    # A delayed marker must never be written after baseline has returned.
    (tmp_path / "slow.py").write_text(
        "from pathlib import Path\nimport time\n"
        "time.sleep(60)\nPath('late.txt').write_text('still running')\n"
    )
    command = f"{shlex.quote(sys.executable)} slow.py"
    assert cli.main(["adopt", "baseline", "--repo-root", str(tmp_path), "--command", command,
                     "--timeout", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["command"] == command
    assert report["passed"] is False
    assert report["exit_code"] != 0
    assert report["failing"] == []
    assert not (tmp_path / "late.txt").exists()


def test_pytest_keeps_parameterized_node_ids_and_quotes_acceptance_path(tmp_path):
    (tmp_path / ".foragent.yml").write_text("acceptance_dir: locked checks/\n")
    locked = tmp_path / "locked checks"
    locked.mkdir()
    (locked / "test_locked.py").write_text("def test_locked():\n    assert False\n")
    (tmp_path / "test_bad.py").write_text(
        "import pytest\n@pytest.mark.parametrize('value', [0], ids=['with spaces'])\n"
        "def test_bad(value):\n    assert value\n"
    )
    report = adopt.baseline(tmp_path, f"{shlex.quote(sys.executable)} -m pytest --color=yes", timeout=10)
    assert report["passed"] is False
    assert report["failing"] == ["test_bad.py::test_bad[with spaces]"]
    assert "--ignore=locked checks" in shlex.split(report["command"])
