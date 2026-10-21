"""Acceptance: `fa adopt survey --json` describes a repository without changing it."""

import json
from pathlib import Path

from foragent import cli

from conftest import GitHistory, git_command


def survey(root: Path, capsys) -> dict:
    assert cli.main(["adopt", "survey", "--repo-root", str(root), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def clone_of(tmp_path: Path, commits: list[dict]) -> Path:
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    history = GitHistory(origin, clone)
    for files in commits:
        history.update(files)
    git_command(tmp_path, "clone", str(origin), str(clone))
    return clone


def test_existing_project_reports_hot_paths_setup_and_ci(tmp_path, capsys):
    workflow = "on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: uv run pytest -q\n"
    root = clone_of(tmp_path, [
        {"src/app/core.py": "a = 1\n", "docs/guide.md": "x\n", "CLAUDE.md": "rules\n",
         ".claude/agents/helper.md": "---\nname: helper\n---\n", ".github/workflows/ci.yml": workflow},
        {"src/app/core.py": "a = 2\n"},
        {"src/app/util.py": "b = 1\n"},
    ])
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}

    report = survey(root, capsys)

    assert report["kind"] == "existing"
    assert report["hot_paths"][0] == {"path": "src/app", "commits": 3}
    assert {"path": "docs", "commits": 1} in report["hot_paths"]
    assert "CLAUDE.md" in report["existing_setup"] and ".claude/" in report["existing_setup"]
    assert report["ci"] == [{"file": ".github/workflows/ci.yml", "runs_acceptance": True}]
    after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
    assert after == before
    assert git_command(root, "status", "--porcelain") == ""


def test_ci_that_ignores_acceptance_dir_is_not_flagged(tmp_path, capsys):
    workflow = "jobs:\n  t:\n    steps:\n      - run: uv run pytest -q --ignore=tests/acceptance\n"
    root = clone_of(tmp_path, [{"pkg/main.py": "x = 1\n", ".github/workflows/ci.yml": workflow}])
    assert survey(root, capsys)["ci"] == [{"file": ".github/workflows/ci.yml", "runs_acceptance": False}]


def test_readme_only_repository_is_empty(tmp_path, capsys):
    root = clone_of(tmp_path, [{"README.md": "# New\n", ".gitignore": "*.pyc\n"}])
    report = survey(root, capsys)
    assert report["kind"] == "empty"
    assert report["ci"] == []
    assert report["existing_setup"] == []


def test_repository_without_commits_is_empty(tmp_path, capsys):
    git_command(tmp_path, "init", "--initial-branch=main")
    report = survey(tmp_path, capsys)
    assert report["kind"] == "empty"
    assert report["hot_paths"] == []
