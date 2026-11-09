"""The ideas shelf keeps its own lifecycle apart from the boundary flush (#2958 review)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from foragent import decision_sweep, order_notes  # noqa: E402
from test_ideas_shelf_place import brief_ideas, note, run, status, world  # noqa: E402,F401
from test_order import publishing  # noqa: E402,F401 (fixture)
from test_skills_autosync import isolated_home  # noqa: E402,F401 (fixture)


def _flush(case):
    code, out = run(case, "order", "flush", "--boundary", "next-order")
    assert code == 0, f"fa order flush failed: {out}"


def test_a_boundary_flush_publishes_an_idea_but_leaves_it_on_the_ideas_shelf(world):
    case = world
    ident = note(case, "idea", "Opaque flushed idea")
    _flush(case)
    row = status(case, ident)
    assert row["status"] == "published" and row["published"] is True, row
    assert brief_ideas(case)["count"] == 1, "A flush is not a citation, a confirmation or a drop"


def test_a_flushed_idea_dropped_or_cited_shows_cleared_with_its_reason(world):
    case = world
    dropped = note(case, "idea", "Opaque idea dropped after flush")
    cited = note(case, "idea", "Opaque idea cited after flush")
    _flush(case)
    code, out = run(case, "order", "drop", dropped, "--reason", "Opaque flushed reason")
    assert code == 0, out
    order_notes.cite(_store(case), {"id": "2026-11-09-cites", "spec_markdown": f"Builds {cited}."})
    assert status(case, dropped)["status"] == "cleared"
    assert status(case, dropped)["cleared"]["reason"] == "Opaque flushed reason"
    assert status(case, dropped)["published"] is True, "The flush fact stays beside the cleared status"
    assert status(case, cited)["cleared"]["order"] == "2026-11-09-cites"
    assert brief_ideas(case)["count"] == 0


def test_a_cleared_idea_still_waits_at_its_boundary_until_flushed(world):
    case = world
    ident = note(case, "idea", "Opaque idea dropped before flush")
    assert run(case, "order", "drop", ident, "--reason", "Opaque reason")[0] == 0
    from foragent import shelf

    assert ident in shelf._inventory(_store(case))["notes"], "Dropping is not the boundary's publication"
    _flush(case)
    assert ident not in shelf._inventory(_store(case))["notes"]
    assert status(case, ident)["status"] == "cleared"


def test_a_confirmed_candidate_citing_an_idea_clears_it(world):
    case = world
    cited = note(case, "idea", "Opaque idea a candidate cites")
    other = note(case, "idea", "Opaque idea nobody confirms")
    store = _store(case)
    unconfirmed = order_notes.append(store, kind="decision", boundary="decision-sweep",
                                     content=f"Opaque candidate naming {other}")
    candidate = order_notes.append(store, kind="decision", boundary="decision-sweep",
                                   content=f"Opaque candidate naming {cited}")
    assert status(case, cited)["status"] == "waiting", "An unconfirmed candidate clears nothing"
    decision_sweep.confirm(store, candidate["id"], "Adopt the storage plan", "next-order", "en")
    row = status(case, cited)
    assert row["status"] == "cleared" and row["cleared"]["candidate"] == candidate["id"], row
    assert status(case, other)["status"] == "waiting", unconfirmed
    assert brief_ideas(case)["count"] == 1


def _store(case):
    from foragent.commands import repo_store

    return repo_store(case.root)[2]  # The store the fa commands above wrote to.
