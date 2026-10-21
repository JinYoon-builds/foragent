"""Acceptance: `fa bench record` appends one measured result row per arm run."""

import json
from pathlib import Path

import pytest

from foragent import cli


def scenario(tmp_path: Path) -> Path:
    path = tmp_path / "scenario"
    path.mkdir()
    (path / "scenario.yml").write_text(
        "name: words\nbrief: Count words in a file.\n"
        "checks:\n  - test -f done.txt\n  - test -f \"$FA_BENCH_SCENARIO/scenario.yml\"\n  - 'false'\n",
        encoding="utf-8")
    return path


def product(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    repo.mkdir()
    (repo / "done.txt").write_text("ok\n", encoding="utf-8")
    return repo


def run_line(number: int, started: str, attempts: int, outcome: str) -> str:
    return json.dumps({"number": number, "branch": f"foragent/issue-{number}", "worktree": "/w", "pid": None,
                       "started_at": started, "last_progress_at": started, "attempts": attempts,
                       "status": "finished", "outcome": outcome})


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]


def test_record_with_gate_history(tmp_path):
    path, repo = scenario(tmp_path), product(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "runs.jsonl").write_text("\n".join([
        run_line(1, "2026-10-20T08:05:00+00:00", 1, "merged"),
        run_line(2, "2026-10-20T08:10:00+00:00", 3, "merged"),
        run_line(3, "2026-10-19T08:10:00+00:00", 2, "merged"),  # before this bench run
    ]) + "\n", encoding="utf-8")
    assert cli.main(["bench", "record", str(path), "--arm", "B", "--repo", str(repo),
                     "--started", "2026-10-20T08:00:00+00:00", "--finished", "2026-10-20T08:30:00+00:00",
                     "--state-dir", str(state), "--tokens", "120000", "--interventions", "1"]) == 0
    [row] = rows(path)
    assert row["scenario"] == "words" and row["arm"] == "B"
    assert row["elapsed_seconds"] == 1800
    assert row["checks_passed"] == 2 and row["checks_total"] == 3
    assert row["runs"] == 2 and row["merged"] == 2 and row["retries"] == 2
    assert row["tokens"] == 120000 and row["interventions"] == 1


def test_arm_without_gate_leaves_gate_fields_empty_and_appends(tmp_path):
    path, repo = scenario(tmp_path), product(tmp_path)
    for _ in range(2):
        assert cli.main(["bench", "record", str(path), "--arm", "A", "--repo", str(repo),
                         "--started", "2026-10-20T08:00:00+00:00",
                         "--finished", "2026-10-20T08:10:00+00:00"]) == 0
    first, second = rows(path)
    assert first["arm"] == second["arm"] == "A"
    assert first["retries"] is None and first["runs"] is None
    assert first["tokens"] is None and first["interventions"] is None
    assert first["elapsed_seconds"] == 600


def test_invalid_arm_or_missing_scenario_is_rejected(tmp_path):
    repo = product(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["bench", "record", str(scenario(tmp_path)), "--arm", "D", "--repo", str(repo),
                  "--started", "2026-10-20T08:00:00+00:00"])
    assert cli.main(["bench", "record", str(tmp_path / "missing"), "--arm", "A", "--repo", str(repo),
                     "--started", "2026-10-20T08:00:00+00:00"]) != 0
