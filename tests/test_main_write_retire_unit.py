"""Retiring a blocked main write by hand (q86): who, and how long status shows it.

The locked acceptance test covers the CLI and the owner pass. These cases pin the
choices it leaves open: the row records who retired it, status shows the retired
row only until a later request lands, a retire of an unknown id refuses, and
automatic retirements (supersede, closed dream PR) are not shown.

Break-it: dropping `retired_by` fails the record case; showing retired rows forever
fails the drop-out case; hiding them always fails the shown case.
"""

from pathlib import Path

import pytest

from foragent import main_write_requests as writes
from foragent.state import StateStore


def _row(store, status, **extra):
    row = writes.submit(store, "main", f"write {status}", "lane orch", {"kind": "files", "files": {}})
    row.update(status=status, **extra)
    writes._save(store, row)
    return row


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state")


def test_retire_records_reason_and_who(store):
    stuck = _row(store, "blocked", error="changed since approval")
    writes.retire(store, stuck["id"], "must not land", "jin")
    row = next(r for r in writes.requests(store) if r["id"] == stuck["id"])
    assert (row["status"], row["reason"], row["retired_by"]) == ("retired", "must not land", "jin")


def test_status_shows_a_retired_row_until_a_later_request_lands(store):
    stuck = _row(store, "blocked")
    later = _row(store, "pending")
    writes.retire(store, stuck["id"], "must not land", "orchestrator")
    shown = {r["id"]: r for r in writes.summary(store)}
    assert shown[stuck["id"]]["reason"] == "must not land"
    assert shown[stuck["id"]]["retired_by"] == "orchestrator"
    assert "reason" not in shown[later["id"]]

    later.update(status="done")
    writes._save(store, later)
    assert writes.summary(store) == []


def test_automatic_retirements_stay_hidden(store):
    _row(store, "retired", reason="Superseded by later snapshot x")
    assert writes.summary(store) == []


def test_unknown_id_is_refused_without_writing(store):
    _row(store, "blocked")
    before = {p.name: p.read_bytes() for p in Path(store.root, "main-writes").glob("*.json")}
    with pytest.raises(writes.Refused, match="nope"):
        writes.retire(store, "nope", "why", "orchestrator")
    assert {p.name: p.read_bytes() for p in Path(store.root, "main-writes").glob("*.json")} == before
