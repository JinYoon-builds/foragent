"""Spend uses the ETA size basis; drift uses recorded file sets, never prose judgment."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import capacity, orch_events, orch_facts
from foragent.config import Config
from foragent.state import StateStore


NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def record(store, clock, number, seconds, rounds, *, lane="orch", declared=None, changed=None):
    clock[0] = NOW - timedelta(hours=2) - timedelta(seconds=seconds)
    store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number), lane=lane, tier="s")
    store.update_run(number, declared_files=declared, changed_files=changed)
    # Issue size is independent of the executor's tier.
    store.update_local_issue(number, title=f"Task {number}", body="", labels=["size:m"], state="closed")
    for round_number in range(1, rounds + 1):
        at = clock[0] + timedelta(seconds=round_number)
        path = store.root / "runs" / f"issue-{number}" / f"review-{round_number}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VERDICT: PASS\n")
        import os
        os.utime(path, (at.timestamp(), at.timestamp()))
        orch_events.append_event(store, at, number, "review", "PASS 0 findings", verdict="PASS",
                                 findings=0, round=round_number, source="reviewer")
    clock[0] += timedelta(seconds=seconds)
    store.finish_run(number, "merged")
    clock[0] = NOW


@pytest.mark.parametrize("missing", [False, True])
def test_facts_publish_exact_spend_and_file_drift_per_run_and_lane(tmp_path, monkeypatch, missing):
    clock = [NOW]
    store = StateStore(tmp_path / "machine" / "project", now=lambda: clock[0])
    monkeypatch.setattr(capacity, "machine_root", lambda: store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    config = Config(repo="owner/project", skill_libraries=[], language="en")
    if not missing:
        for number, seconds, rounds in [(1, 600, 1), (2, 900, 2), (3, 1200, 3)]:
            record(store, clock, number, seconds, rounds, lane="order", declared=[], changed=[])
    record(store, clock, 9, 1800, 4,
           declared=None if missing else ["src/foragent/orch*.py", "tests/test_orch.py"],
           changed=None if missing else ["src/foragent/orch.py", "src/unplanned.py", "src/unplanned.py"])
    before = {str(path.relative_to(store.root)): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    result = orch_facts.collect(store, config, tmp_path)
    row, = [row for row in result["runs"] if row["issue"] == 9]
    assert "spend" in row and "scope_drift" in row, "Facts omitted the architect's review inputs"
    spend = row["spend"]
    assert spend["size"] == "m" and spend["actual_seconds"] == 1800 and spend["actual_rounds"] == 4
    assert spend["typical_seconds"] == (5400 if missing else 1050)
    assert spend["basis_runs"] == (None if missing else 4)
    assert spend["runtime_ratio"] == pytest.approx(1800 / spend["typical_seconds"])
    assert spend["typical_rounds"] == (None if missing else 2.5)
    assert spend["rounds_ratio"] == (None if missing else 1.6)
    assert row["scope_drift"] == ({"declared": None, "changed": None, "outside_declared": None} if missing else {
        "declared": ["src/foragent/orch*.py", "tests/test_orch.py"],
        "changed": ["src/foragent/orch.py", "src/unplanned.py"], "outside_declared": ["src/unplanned.py"]})
    area = result["areas"]["orch"]
    assert area["executions"] == 1
    assert area["spend"]["actual_seconds"] == 1800
    assert area["spend"]["typical_seconds"] == spend["typical_seconds"]
    assert area["scope_drift"] == {"9": row["scope_drift"]}
    human = orch_facts.human(result, "en").lower()
    assert "spend" in human and "scope" in human
    assert {str(path.relative_to(store.root)): path.read_bytes() for path in store.root.rglob("*") if path.is_file()} == before
