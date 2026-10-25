"""Acceptance test for ci-mode-facts. Locked by Foragent; do not edit."""

import json

import pytest

from test_ci_mode import comparison_record, mode_case, run


@pytest.mark.parametrize("mode", ["local", "ci"])
def test_facts_exposes_persisted_comparison_location_and_duration_for_real_merge(
    git_history, tmp_path, monkeypatch, capsys, mode,
):
    case = mode_case(git_history, tmp_path, monkeypatch, mode=mode)
    assert run(case) == "merged"
    main = bind_cli(case, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("fa orch facts must only read the completed merge's local records")

    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in case.store.root.rglob("*") if path.is_file()}
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    capsys.readouterr()
    assert cli_exit(main, ["orch", "facts", "--repo-root", str(case.root), "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    row = next(row for row in facts["runs"] if row["issue"] == 1)
    assert row["comparison"] == comparison_record(case, mode)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in case.store.root.rglob("*") if path.is_file()} == before
