"""Acceptance test for overlap-facts. Locked by Foragent; do not edit."""

from datetime import timedelta

from test_optimistic_overlap import CLI, OTHER, STATE, _overlap, _recorded, offline_overlap  # noqa: F401
from test_orch_facts import NOW, completed, facts, facts_case
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


def test_optimistic_overlap_facts_reports_per_file_rates_redos_and_measured_file_counts(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    _recorded(case, 21, hours=3, declared=[STATE, CLI, OTHER], changed=[STATE],
              overlaps=[_overlap(STATE, issues=(90, 91))])
    _recorded(case, 21, hours=2, declared=[STATE, CLI], changed=[STATE, CLI], overlaps=[
        _overlap(STATE, conflict=True, seconds=13, redo=1), _overlap(CLI)])
    # A non-overlapping merge is not evidence for the optimistic conflict rate.
    _recorded(case, 22, hours=1, declared=[STATE], changed=[STATE], overlaps=[])
    # Nor is an unfinished/failed run, even if conflict resolution was attempted.
    _recorded(case, 23, hours=1, declared=[STATE], changed=[STATE],
              overlaps=[_overlap(STATE, conflict=True, seconds=99, redo=3)], outcome="error")
    _recorded(case, 99, hours=40 * 24, declared=[STATE], changed=[STATE],
              overlaps=[_overlap(STATE, conflict=True, seconds=999, redo=9)])
    completed(case, 24, started=NOW - timedelta(hours=1))  # Legacy, unmeasured Files.

    result = facts(case, capsys)

    assert result["file_conflicts"] == {
        STATE: {"overlapping_merges": 2, "conflicted_merges": 1, "conflict_rate": 0.5,
                "resolution_seconds": 13, "redo_count": 1},
        CLI: {"overlapping_merges": 1, "conflicted_merges": 0, "conflict_rate": 0,
              "resolution_seconds": 0, "redo_count": 0},
    }, "Deduplicate snapshot/history by execution; count peers once; attribute conflicts to their file"
    rows = [row for row in result["runs"] if row["issue"] == 21]
    assert sorted((row["declared_file_count"], row["changed_file_count"]) for row in rows) == [(2, 2), (3, 1)]
    assert sorted(len(row["overlap_records"]) for row in rows) == [1, 2]
    legacy, = [row for row in result["runs"] if row["issue"] == 24]
    assert legacy["declared_file_count"] is None and legacy["changed_file_count"] is None
    assert legacy["overlap_records"] == []
    assert facts(case, capsys) == result, "Observing facts twice must be read-only and deterministic"


def test_optimistic_overlap_facts_human_output_names_the_file_rate_and_redo_cost(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    _recorded(case, 196, hours=2, declared=[STATE, CLI], changed=[STATE],
              overlaps=[_overlap(STATE, conflict=True, seconds=13, redo=1)])
    _recorded(case, 197, hours=1, declared=[STATE], changed=[STATE], overlaps=[_overlap(STATE)])

    output = facts(case, capsys, human=True)

    lines = [line.lower() for line in output.splitlines() if STATE in line]
    assert any(("50%" in line or "50.0%" in line or "1/2" in line)
               and "redo" in line and "1" in line for line in lines), output
    assert any("13" in line for line in lines), "Show measured resolution time alongside its file"
