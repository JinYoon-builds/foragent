from datetime import UTC, datetime
import json

import pytest

from foragent import bench, cli
from foragent.state import StateStore


@pytest.fixture
def setup(tmp_path):
    scenario = tmp_path / "scenario"
    scenario.mkdir()
    (scenario / "scenario.yml").write_text(
        "name: sample\nbrief: Check the completed work.\nchecks: ['true']\n", encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    return scenario, repo


def record(scenario, repo, *options):
    return cli.main([
        "bench", "record", str(scenario), "--arm", "C", "--repo", str(repo),
        "--started", "2026-10-20T08:00:00+00:00", *options,
    ])


def read_row(scenario):
    return json.loads((scenario / "results.jsonl").read_text(encoding="utf-8"))


def test_history_includes_both_boundaries_and_compares_timezones(setup, tmp_path):
    scenario, repo = setup
    store = StateStore(tmp_path / "state")
    for number, timestamp, attempts, outcome in [
        (1, "2026-10-20T16:59:59+09:00", 9, "merged"),
        (2, "2026-10-20T17:00:00+09:00", 2, "merged"),
        (3, "2026-10-20T17:30:00+09:00", 4, "failed"),
        (4, "2026-10-20T17:30:01+09:00", 9, "merged"),
    ]:
        store._now = lambda: datetime.fromisoformat(timestamp)
        store.start_run(number, branch="bench", worktree=repo)
        store.update_run(number, attempts=attempts)
        store.finish_run(number, outcome)
    assert record(scenario, repo, "--finished", "2026-10-20T08:30:00Z",
                  "--state-dir", str(store.root)) == 0
    row = read_row(scenario)
    assert (row["runs"], row["merged"], row["retries"]) == (2, 1, 4)


def test_default_finished_is_now_and_elapsed_is_an_integer(setup, monkeypatch):
    scenario, repo = setup

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 8, 0, 2, 900000, tzinfo=UTC)

    monkeypatch.setattr(bench, "datetime", Clock)
    assert record(scenario, repo) == 0
    row = read_row(scenario)
    assert row["started"] == "2026-10-20T08:00:00+00:00"
    assert row["finished"] == "2026-10-20T08:00:02.900000+00:00"
    assert type(row["elapsed_seconds"]) is int
    assert row["elapsed_seconds"] == 2
    assert all(row[key] is None for key in ("runs", "merged", "retries", "tokens", "interventions"))


def test_shell_checks_run_in_order_after_failure_with_absolute_scenario(setup, monkeypatch):
    scenario, repo = setup
    (scenario / "scenario.yml").write_text(
        "name: sample\nbrief: Shell checks.\nchecks:\n"
        "  - 'false'\n"
        "  - 'printf ok > done.txt'\n"
        "  - 'test $(cat done.txt) = ok && test -f \"$FA_BENCH_SCENARIO/scenario.yml\"'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(scenario.parent)
    monkeypatch.setenv("FA_BENCH_SCENARIO", "/wrong")
    assert record(scenario.relative_to(scenario.parent), repo,
                  "--finished", "2026-10-20T08:01:00Z") == 0
    row = read_row(scenario)
    assert (row["checks_passed"], row["checks_total"]) == (2, 3)
    assert (repo / "done.txt").read_text() == "ok"


@pytest.mark.parametrize("content", [
    "", "[name, brief, checks]", "name: [", "name: sample\nchecks: []",
    "name: ''\nbrief: b\nchecks: []", "name: sample\nbrief: b\nchecks: true",
    "name: sample\nbrief: b\nchecks: [false]", "name: sample\nbrief: b\nchecks: ['']",
])
def test_invalid_scenario_reports_error_without_running_checks(setup, content, capsys, monkeypatch):
    scenario, repo = setup
    (scenario / "scenario.yml").write_text(content, encoding="utf-8")

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid scenario must be rejected before running shell commands")

    monkeypatch.setattr(bench.subprocess, "run", unexpected)
    assert record(scenario, repo) != 0
    assert capsys.readouterr().err
    assert not (scenario / "results.jsonl").exists()


@pytest.mark.parametrize("finished", ["invalid", "2026-10-20T08:01:00", "2026-10-20T07:59:59Z"])
def test_invalid_time_window_does_not_append(setup, finished, capsys):
    scenario, repo = setup
    assert record(scenario, repo, "--finished", finished) != 0
    assert capsys.readouterr().err
    assert not (scenario / "results.jsonl").exists()


def test_empty_history_records_zero_gate_counts(setup, tmp_path):
    scenario, repo = setup
    assert record(scenario, repo, "--finished", "2026-10-20T08:01:00Z",
                  "--state-dir", str(tmp_path / "state")) == 0
    row = read_row(scenario)
    assert (row["runs"], row["merged"], row["retries"]) == (0, 0, 0)


def test_report_missing_results_needs_no_scenario_definition(tmp_path, capsys):
    assert cli.main(["bench", "report", str(tmp_path), "--json"]) == 0
    arms = json.loads(capsys.readouterr().out)["arms"]
    assert list(arms) == ["A", "B", "C"]
    for summary in arms.values():
        assert summary["records"] == 0
        assert all(value is None for key, value in summary.items() if key != "records")
    assert not (tmp_path / "results.jsonl").exists()


def test_report_medians_exclude_nulls_independently(tmp_path, capsys):
    metrics = ("elapsed_seconds", "checks_passed", "checks_total", "retries", "tokens", "interventions")
    rows = [
        dict(arm="C", **dict(zip(metrics, values)))
        for values in [(9, None, 3, 0, 100, None),
                       (1, 2, 7, None, None, None),
                       (5, 5, None, 3, 0, None)]
    ]
    (tmp_path / "results.jsonl").write_text(
        "\n" + "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8",
    )
    assert cli.main(["bench", "report", str(tmp_path), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)["arms"]["C"]
    assert summary == dict(records=3, **dict(zip(metrics, (5, 3.5, 5, 1.5, 50, None))))
