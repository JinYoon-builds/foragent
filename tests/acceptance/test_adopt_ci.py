"""Acceptance: `fa adopt ci` excludes the acceptance dir from CI pytest runs only when asked."""

import json
from pathlib import Path

from foragent import cli

WORKFLOW = (
    "name: ci\n"
    "on: [push]\n"
    "jobs:\n"
    "  test:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - run: uv run pytest -q\n"
)


def setup(root: Path, workflow: str = WORKFLOW, name: str = "ci.yml") -> Path:
    path = root / ".github/workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow, encoding="utf-8")
    return path


def ci(root: Path, capsys, *extra: str) -> dict:
    assert cli.main(["adopt", "ci", "--repo-root", str(root), "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_report_only_without_flag(tmp_path, capsys):
    path = setup(tmp_path)
    report = ci(tmp_path, capsys)
    assert report["needs_change"] == [".github/workflows/ci.yml"]
    assert report["changed"] == []
    assert path.read_text(encoding="utf-8") == WORKFLOW


def test_exclude_acceptance_edits_only_the_pytest_line_and_is_idempotent(tmp_path, capsys):
    path = setup(tmp_path)
    report = ci(tmp_path, capsys, "--exclude-acceptance")
    assert report["changed"] == [".github/workflows/ci.yml"]
    assert path.read_text(encoding="utf-8") == WORKFLOW.replace(
        "uv run pytest -q\n", "uv run pytest -q --ignore=tests/acceptance\n")
    again = ci(tmp_path, capsys, "--exclude-acceptance")
    assert again["changed"] == [] and again["needs_change"] == []
    assert path.read_text(encoding="utf-8").count("--ignore=tests/acceptance") == 1


def test_custom_acceptance_dir(tmp_path, capsys):
    (tmp_path / ".foragent.yml").write_text("acceptance_dir: checks/locked/\n", encoding="utf-8")
    path = setup(tmp_path)
    ci(tmp_path, capsys, "--exclude-acceptance")
    assert "      - run: uv run pytest -q --ignore=checks/locked\n" in path.read_text(encoding="utf-8")


def test_non_pytest_runner_is_reported_for_manual_change(tmp_path, capsys):
    workflow = "jobs:\n  t:\n    steps:\n      - run: npm test\n"
    path = setup(tmp_path, workflow, "node.yml")
    report = ci(tmp_path, capsys, "--exclude-acceptance")
    assert report["changed"] == []
    assert report["manual"] == [{"file": ".github/workflows/node.yml", "line": "npm test"}]
    assert path.read_text(encoding="utf-8") == workflow
