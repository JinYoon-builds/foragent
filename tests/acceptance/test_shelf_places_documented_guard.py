"""Regression controls for the shelf-place documentation check (not in Done when; pass on main).

They prove the check itself: it reads the real place list and command tree, accepts a place
named with a real command in a shelf section, and refuses a missing, wrong, group-only or
off-topic mention.
"""

import shelf_places_docs_support as support
from foragent import shelf


def test_the_required_places_are_in_the_shelf_place_list():
    listed = {place.name for place in shelf.places()}
    # 'ideas' arrives with handoff-ideas-place; the Done-when idea test checks it.
    assert set(support.REQUIRED) - {support.IDEAS} <= listed, sorted(listed)


def test_the_command_tree_is_read_through_fa_help():
    assert support.is_command(("orch", "promise", "add"))
    assert support.is_command(("order", "note"))
    assert not support.is_command(("orch", "promise"))  # a group, not a command
    assert not support.is_command(("orch", "promise", "nosuch"))
    assert support.commands("Run `fa order note --defer x --kind idea --text -`.") == {"fa order note"}
    assert support.commands("see /fa report and fa-orchestrator") == set()


def _with(text, place="promises", command=None):
    return support.documented(place, [text], command)


def test_a_place_named_with_its_command_in_a_shelf_section_is_documented():
    text = "# Shelf places\n\nThe `promises` place: record one with `fa orch promise add <when> <do>`.\n"
    assert _with(text)
    assert _with(text, command="fa orch promise add")
    table = "## The shelf\n\n| place | fed by |\n|---|---|\n| `notes` | `fa order note` |\n| `promises` | `fa orch promise add` |\n"
    assert _with(table, command="fa orch promise add")


def test_missing_wrong_group_or_off_topic_mentions_are_refused():
    assert not _with("# Shelf\n\nThe `promises` place exists.\n")  # no command
    assert not _with("# Shelf\n\n`promises`: `fa orch promise list`.\n", command="fa orch promise add")  # wrong one
    assert not _with("# Shelf\n\n`promises`: `fa orch promise`.\n")  # only a group
    assert not _with("# Shelf\n\n`promises`: `fa orch promise nosuch`.\n")  # not a command
    assert not _with("# Merge\n\n`promises`: `fa orch promise add`.\n")  # no shelf in the section
    assert not _with("# Shelf\n\nThe promises place.\n\nRun `fa orch promise add`.\n")  # separate paragraphs
    assert not _with("# Shelf\n\n`promises-old`: `fa orch promise add`.\n")  # another name


def test_the_check_names_an_added_place_when_docs_name_everything_else(monkeypatch):
    added = support.new_place("zz-unannounced-place")
    monkeypatch.setattr(shelf, "places", lambda: (added,))
    found = support.problems()
    assert found and {name for name, _ in found} == {added.name}
    assert all(added.name in message for _, message in found)


def test_an_added_place_documented_in_map_and_skill_passes(monkeypatch):
    added = support.new_place("zz-announced-place")
    section = "## Shelf\n\nThe `zz-announced-place` place is fed by `fa orch promise add`.\n"
    monkeypatch.setattr(support, "map_texts", lambda: [section])
    monkeypatch.setattr(support, "skill_texts", lambda session: [section] if session == "orchestrator" else [])
    assert support.problems((added,)) == []
    assert support.idea_documented([
        "## Shelf\n\n`ideas`: `fa order note --defer <b> --kind idea --text -` (waiting ideas).\n"])
    assert not support.idea_documented(["## Shelf\n\n`ideas`: `fa order note --defer <b> --kind rule`.\n"])
    assert not support.idea_documented(["## Shelf\n\n`notes`: `fa order note --defer <b> --kind idea`.\n"])
