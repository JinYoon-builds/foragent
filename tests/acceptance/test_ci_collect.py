"""Acceptance test for ci-collect (D52). Locked by Foragent; do not edit."""

from pathlib import Path

import yaml


def _runs(path):
    jobs = yaml.safe_load(path.read_text())["jobs"]
    return [step["run"] for job in jobs.values() for step in job["steps"] if "run" in step]


def test_project_ci_collects_every_test_including_acceptance():
    runs = _runs(Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml")
    collect = [run for run in runs if "--collect-only" in run]
    assert collect, runs
    assert all("--ignore" not in run for run in collect)
    # The unit run excludes acceptance tests, either with --ignore or through the
    # shard entry point that discovers unit files only (#561).
    assert any(("--ignore=tests/acceptance" in run or "foragent.ci_unit_shard" in run)
               and "--collect-only" not in run for run in runs)


def test_adopt_ci_reports_a_missing_collect_step(tmp_path):
    from foragent.adopt import ci

    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: uv run pytest -q --ignore=tests/acceptance\n"
    )
    report = ci(tmp_path)
    assert report["collect_only"] == [{"file": ".github/workflows/ci.yml",
                                       "add": "uv run pytest --collect-only -q"}]

    workflow.write_text(workflow.read_text() + "      - run: uv run pytest --collect-only -q\n")
    assert ci(tmp_path)["collect_only"] == []
