"""Acceptance tests for shelf-places-documented (Jin r1317).

A session can only use a shelf place it has been told about. Every place in the shelf's place
list (``foragent.shelf.places()``), with the fa command that feeds it, is named in
docs/mechanisms.md and in the skill of the session that feeds it: skills/fa or skills/fa-order for
planning, skills/fa-orchestrator for the orchestrator. The promise place (``fa orch promise add``)
and the idea place (``fa order note --kind idea``), missing today, are added. A place added to the
list without those mentions fails this check, which names the place.

The rule for "named with its command" is in shelf_places_docs_support.py.
"""

from shelf_places_docs_support import (
    IDEAS, MAP, SESSIONS, idea_documented, map_problems, map_texts, new_place, problems, skill_problems, skill_texts,
)
from foragent import shelf


def _report(found):
    return "\n".join(message for _, message in found)


def test_every_shelf_place_is_named_with_its_feeding_command_in_the_mechanisms_map():
    places = shelf.places()
    assert places, "The shelf defines places"
    found = map_problems(places)
    assert not found, f"{MAP} must name every shelf place with the fa command that feeds it:\n{_report(found)}"


def test_every_shelf_place_is_named_with_its_command_in_the_feeding_sessions_skill():
    found = skill_problems(shelf.places())
    assert not found, ("The skill of the session that feeds each shelf place must name it with its fa command:\n"
                       f"{_report(found)}")


def test_the_promise_place_is_documented_with_fa_orch_promise_add():
    found = [message for name, message in problems() if name == "promises"]
    assert not found, ("The promise place must be named with `fa orch promise add` in the map, the planning "
                       f"skill and the orchestrator skill:\n" + "\n".join(found))


def test_the_idea_place_is_documented_with_fa_order_note_kind_idea():
    listed = {place.name for place in shelf.places()}
    assert IDEAS in listed, f"The shelf place {IDEAS!r} (handoff-ideas-place) is in the place list: {sorted(listed)}"
    found = [message for name, message in problems() if name == IDEAS]
    assert not found, "\n".join(found)
    assert idea_documented(map_texts()), (
        f"{MAP} must name the shelf place 'ideas' with `fa order note --kind idea`")
    planning = SESSIONS["planning"]
    assert idea_documented(skill_texts("planning")), (
        f"The planning skill ({', '.join(planning)}) must name the shelf place "
        "'ideas' with `fa order note --kind idea`")


def test_a_place_added_without_its_mentions_fails_the_check_and_is_named(monkeypatch):
    added = new_place("zz-unannounced-place")
    installed = shelf.places()
    monkeypatch.setattr(shelf, "places", lambda: (*installed, added))
    found = problems()
    failing = {name for name, _ in found}
    assert failing == {added.name}, (
        "Only the newly added, undocumented place may fail the documentation check; "
        f"failing places: {sorted(failing)}\n{_report(found)}")
    assert all(added.name in message for _, message in found), "Each failure names the place"
    where = " ".join(message for _, message in found)
    assert MAP in where, "The check says the map is missing the place"
    assert "skill" in where, "The check says the skill is missing the place"
