"""Acceptance tests for docs-signal-new-mechanism (order: mechanism docs complete, Jin r1317).

A merge that adds a new fa subcommand or a new shelf place raises the docs owner's
(the docs role's) role signal naming it; a merge that adds neither raises none.

Chosen public contract (the existing role-signal worlds):
- foragent.orch_role_raise.raise_due(store, repo_root, now) is the gate tick's raise step;
  a raised role signal is an orchestrator inbox request with role=<name>.
- The fa command tree is read from the real parser (foragent.cli), the place list from
  foragent.shelf.places(); the world changes them the way a merge would.
- fa orch signal-answer <id> called --why TEXT answers a raised signal.
"""

from datetime import timedelta

from docs_signal_world import (add_group_command, add_nested_command, add_place, docs_requests,
                               docs_world, merge, names, tick)
from test_role_signal_raise import answer
from test_role_source import NOW
from test_skills_autosync import isolated_home  # noqa: F401 (fixture)

HOUR = timedelta(hours=1)


def test_a_merge_adding_a_nested_fa_subcommand_raises_the_docs_signal_naming_it(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == [], "Before any merge adds a mechanism, the docs owner is not signalled"
    add_nested_command(case, "orch", "docsprobe")
    merge(case, 1, NOW + HOUR, ["src/foragent/orch_docsprobe.py", "src/foragent/commands/_orch_box.py"])
    raised = tick(case, NOW + HOUR + timedelta(minutes=5))
    rows = docs_requests(case)
    assert len(raised) == 1 and len(rows) == 1, (
        f"A merge that adds `fa orch docsprobe` raises exactly one docs-owner signal: {rows}")
    text = rows[0]["text"]
    assert names(text, "docsprobe") and "orch docsprobe" in text, (
        f"The docs-owner signal names the new subcommand `fa orch docsprobe`: {text!r}")
    assert rows[0]["status"] != "done"
    assert tick(case, NOW + HOUR + timedelta(minutes=10)) == [], "The same new subcommand is signalled once"
    assert len(docs_requests(case)) == 1


def test_a_merge_adding_a_top_level_fa_command_raises_the_docs_signal_naming_it(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == []
    add_group_command(case, tmp_path, "docsprobegroup")
    try:
        merge(case, 1, NOW + HOUR, ["src/foragent/commands/docsprobegroup.py"])
        raised = tick(case, NOW + HOUR + timedelta(minutes=5))
        rows = docs_requests(case)
        assert len(raised) == 1 and len(rows) == 1, (
            f"A merge that adds `fa docsprobegroup` raises exactly one docs-owner signal: {rows}")
        assert names(rows[0]["text"], "docsprobegroup"), (
            f"The docs-owner signal names the new command: {rows[0]['text']!r}")
        assert tick(case, NOW + HOUR + timedelta(minutes=10)) == []
    finally:
        case.forget()


def test_a_merge_adding_a_shelf_place_raises_the_docs_signal_naming_it_and_each_later_place_again(
        tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == []
    add_place(case, "probeplaces")
    merge(case, 1, NOW + HOUR, ["src/foragent/shelf.py"])
    raised = tick(case, NOW + HOUR + timedelta(minutes=5))
    rows = docs_requests(case)
    assert len(raised) == 1 and len(rows) == 1, (
        f"A merge that adds the shelf place `probeplaces` raises exactly one docs-owner signal: {rows}")
    first = rows[0]
    assert names(first["text"], "probeplaces"), f"The signal names the new place: {first['text']!r}"
    assert tick(case, NOW + HOUR + timedelta(minutes=10)) == [], "The same new place is signalled once"

    # The docs owner checked the map; a later merge with another place is new evidence.
    case.clock[0] = NOW + 2 * HOUR
    assert answer(case, first["id"], "called", "docs owner checked the mechanisms map") == 0
    assert tick(case, NOW + 3 * HOUR) == [], "An answered signal with nothing new stays quiet"
    add_place(case, "laterplaces")
    merge(case, 2, NOW + 4 * HOUR, ["src/foragent/shelf.py"])
    again = tick(case, NOW + 4 * HOUR + timedelta(minutes=5))
    assert len(again) == 1 and again[0]["id"] != first["id"], (
        f"A second merge adding another place raises the docs-owner signal again: {docs_requests(case)}")
    assert names(again[0]["text"], "laterplaces"), f"The new signal names the later place: {again[0]['text']!r}"
    assert tick(case, NOW + 4 * HOUR + timedelta(minutes=10)) == []
    assert len(docs_requests(case)) == 2


def test_a_merge_adding_neither_raises_no_docs_signal_while_one_that_adds_a_place_does(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == []
    # Edits to the very files that define commands and places, adding none of either.
    merge(case, 1, NOW + HOUR, ["src/foragent/shelf.py", "src/foragent/commands/orch.py", "src/foragent/cli.py"])
    assert tick(case, NOW + HOUR + timedelta(minutes=5)) == [], (
        "A merge that adds no subcommand and no shelf place raises no docs-owner signal")
    assert tick(case, NOW + 2 * HOUR) == []
    assert docs_requests(case) == []
    # Same world, same files: only the added place makes the difference.
    add_place(case, "probeplaces")
    merge(case, 2, NOW + 3 * HOUR, ["src/foragent/shelf.py"])
    raised = tick(case, NOW + 3 * HOUR + timedelta(minutes=5))
    assert len(raised) == 1 and names(raised[0]["text"], "probeplaces"), (
        f"The world that stayed quiet signals as soon as a merge adds a place: {docs_requests(case)}")
