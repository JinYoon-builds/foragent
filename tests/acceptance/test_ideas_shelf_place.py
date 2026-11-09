"""Acceptance tests for handoff-ideas-place: waiting idea notes are a shelf place the PM triages.

Public contract these tests read (choices made for this issue):

- Idea notes are recorded as today: ``fa order note --defer <boundary> --kind idea --text <t> --json``
  prints the note with its ``id``.
- ``foragent.shelf.places()`` holds a place named ``ideas``, read by the PM. ``fa brief --json`` shows its row under ``shelves`` (``count``, ``in_24h``,
  ``out_24h``, ``oldest_minutes``); ``count`` is the number of waiting idea notes. Decision and
  rule notes are not ideas.
- Over a limit means more waiting ideas than the place's ``alarm["count"]`` or an oldest waiting
  idea older than its ``alarm["age"]`` (the limits are read from the place object). An alarm is one
  orchestrator request box record ``from: gate``, ``kind: shelf-alarm``, ``place: ideas``,
  ``to: pm`` (the PM reads this place, so its alarm reaches the PM); one
  over-limit episode raises one such record, however many ticks see it.
- An idea leaves the place when a published order cites it: the order's text (``spec_markdown``)
  names the idea's id. Publication without ``--approve`` is a published order; ``--dry-run`` is not.
- The PM drops an idea with ``fa order drop <id> --reason <text>``. A drop without a reason is
  refused (failing exit) and the idea keeps waiting.
- A cleared idea stays in the log: ``fa order pending --json`` still lists it once, with
  ``status: "cleared"``; a dropped idea's row carries the reason, a cited idea's row names the
  citing order's id.

Every check goes through public entry points: the real ``fa`` CLI with offline trackers and Git,
real gate ticks (``scheduler.tick``) and ``fa orch inbox --json``. No process is started and no
signal is sent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from shelf_object_support import shelf_module
from test_board_status_groups import offline_github  # noqa: F401 (fixture)
from test_order import publishing  # noqa: F401 (fixture)
from test_order_machine_budget import fake_workloads  # noqa: F401 (fixture)
from test_publish_out_of_scope import publish_cli
from test_shelf_requests_notes_lessons import at_wall_clock, cli_json, inbox, later, shelf, tick  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)


# ---------------------------------------------------------------- helpers


def ideas_place():
    shelf = shelf_module()
    place = next((item for item in shelf.places() if item.name == "ideas"), None)
    assert place is not None, (
        f"The shelf has no place named 'ideas': {[item.name for item in shelf.places()]}")
    return place


def ideas_row(shown):
    shelves = shown.get("shelves")
    assert shelves is not None, f"fa brief --json shows no shelves: {sorted(shown)}"
    row = shelves.get("ideas") if isinstance(shelves, dict) else next(
        (item for item in shelves if item.get("name") == "ideas"), None)
    assert row is not None, f"fa brief --json does not show the 'ideas' place: {shelves}"
    for key in ("count", "in_24h", "out_24h", "oldest_minutes"):
        assert key in row, f"The ideas place shows no {key}: {row}"
    return row


def idea_alarms(case):
    """Every shelf alarm for the ideas place in the request box, open or done, with its addressee."""
    rows = [row for row in inbox(case)
            if row.get("from") == "gate" and row.get("kind") == "shelf-alarm" and row.get("place") == "ideas"]
    assert all(row.get("to") == "pm" for row in rows), f"The ideas place's alarm goes to the PM who reads it: {rows}"
    return rows


def values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from values(value)
    elif isinstance(node, list):
        for value in node:
            yield from values(value)
    elif node is not None:
        yield str(node)


# ---------------------------------------------------------------- an offline publishing world


@pytest.fixture
def world(publishing, monkeypatch, capsys):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    main = publish_cli((root, config, tracker, git, events), monkeypatch)
    return SimpleNamespace(root=root, tracker=tracker, events=events, main=main, capsys=capsys)


def run(case, *argv):
    case.capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    output = case.capsys.readouterr()
    return code, output.out + output.err


def note(case, kind, text):
    code, out = run(case, "order", "note", "--defer", "next-order", "--kind", kind, "--text", text, "--json")
    assert code == 0, f"fa order note --kind {kind} failed: {out}"
    return json.loads(out)["id"]


def brief_ideas(case):
    code, out = run(case, "brief", "--json")
    assert code == 0, f"fa brief --json failed: {out}"
    return ideas_row(json.loads(out))


def pending_rows(case, ident):
    code, out = run(case, "order", "pending", "--json")
    assert code == 0, f"fa order pending --json failed: {out}"
    return [row for row in json.loads(out)["notes"] if row.get("id") == ident]


def status(case, ident):
    rows = pending_rows(case, ident)
    assert len(rows) == 1, f"The idea {ident} must stay in the log exactly once: {rows}"
    return rows[0]


def publish_order(case, order_id, spec, *options):
    plan = {"id": order_id, "title": f"Order {order_id}", "summary": "Build the chosen work.",
            "spec_markdown": spec,
            "issues": [{"key": "work", "title": f"Work for {order_id}", "goal": "Deliver the work.",
                        "out_of_scope": "Other work.", "done_when": ["true"], "lane": "api", "size": "s",
                        "acceptance_files": {}}]}
    path = case.root / f"{order_id}.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    code, out = run(case, "order", "publish", str(path), *options)
    assert code == 0, f"fa order publish {' '.join(options)} failed: {out}"
    return out


# ---------------------------------------------------------------- statement 1: listed, one alarm


def test_waiting_idea_notes_are_counted_in_the_ideas_place_and_other_notes_are_not(world):
    case = world
    note(case, "idea", "Opaque idea one Ꮻ")
    note(case, "idea", "Opaque idea two")
    note(case, "decision", "Opaque decision")
    run(case, "order", "note", "--defer", "next-order", "--kind", "rule", "--text", "Opaque new rule",
        "--replace", "Opaque old rule", "--json")
    row = brief_ideas(case)
    assert row["count"] == 2, f"Two waiting idea notes are the ideas place's items (no decision or rule): {row}"
    assert row["oldest_minutes"] is not None, f"A non-empty ideas place shows its oldest item's age: {row}"


def test_ideas_past_the_count_limit_raise_one_shelf_alarm(shelf):
    case = shelf
    limit = ideas_place().alarm["count"]
    at_wall_clock(case)
    for index in range(limit):
        cli_json(case, "order", "note", "--defer", "next-order", "--kind", "idea", "--text", f"Opaque idea {index}")
    tick(case)
    later(case, minutes=1)
    tick(case)
    assert idea_alarms(case) == [], f"{limit} waiting ideas are inside the count limit {limit}"
    cli_json(case, "order", "note", "--defer", "next-order", "--kind", "idea", "--text", "One idea too many")
    later(case, minutes=1)
    tick(case)
    alarms = idea_alarms(case)
    assert len(alarms) == 1, f"{limit + 1} waiting ideas must raise one ideas shelf alarm: {alarms}"
    later(case, minutes=1)
    tick(case)
    assert len(idea_alarms(case)) == 1, "The same over-limit episode must not raise a second ideas alarm"


def test_an_idea_past_the_age_limit_raises_one_shelf_alarm(shelf):
    case = shelf
    age = ideas_place().alarm["age"]
    at_wall_clock(case)
    cli_json(case, "order", "note", "--defer", "next-order", "--kind", "idea", "--text", "Opaque old idea")
    tick(case)
    later(case, seconds=(age - timedelta(minutes=2)).total_seconds())
    tick(case)
    assert idea_alarms(case) == [], f"An idea younger than the age limit {age} raises no ideas alarm"
    later(case, minutes=4)
    tick(case)
    alarms = idea_alarms(case)
    assert len(alarms) == 1, f"An idea waiting past {age} must raise one ideas shelf alarm: {alarms}"
    later(case, minutes=1)
    tick(case)
    assert len(idea_alarms(case)) == 1, "The same over-limit episode must not raise a second ideas alarm"


# ---------------------------------------------------------------- statement 2: leaves, logged as cleared


def test_an_idea_cited_by_a_published_order_leaves_and_is_logged_as_cleared(world):
    case = world
    cited = note(case, "idea", "Opaque cited idea")
    other = note(case, "idea", "Opaque uncited idea")
    assert brief_ideas(case)["count"] == 2

    publish_order(case, "2026-11-09-preview-only", f"# Spec\nBuilds idea {cited}.\n", "--dry-run")
    assert status(case, cited)["status"] == "waiting", "A dry run is not a published order"
    assert brief_ideas(case)["count"] == 2, "A dry run must not take the idea off the place"

    publish_order(case, "2026-11-09-cites-idea", f"# Spec\nBuilds idea {cited}.\n")
    row = status(case, cited)
    assert row["status"] == "cleared", f"An idea cited by a published order is logged as cleared: {row}"
    assert any("2026-11-09-cites-idea" in value for value in values(row)), (
        f"The cleared idea's log row names the order that cited it: {row}")
    assert status(case, other)["status"] == "waiting", "An idea the order does not cite keeps waiting"
    assert brief_ideas(case)["count"] == 1, "Only the cited idea leaves the ideas place"


def test_a_published_order_that_cites_no_idea_clears_nothing(world):
    case = world
    ident = note(case, "idea", "Opaque idea nobody cites")
    publish_order(case, "2026-11-09-no-citation", "# Spec\nUnrelated work.\n")
    assert status(case, ident)["status"] == "waiting"
    assert brief_ideas(case)["count"] == 1


def test_the_pm_drops_an_idea_with_a_reason_and_it_is_logged_as_cleared(world):
    case = world
    dropped = note(case, "idea", "Opaque idea to drop")
    kept = note(case, "idea", "Opaque idea to keep")
    reason = "Opaque reason: superseded by the storage plan"
    code, out = run(case, "order", "drop", dropped, "--reason", reason)
    assert code == 0, f"fa order drop <id> --reason must drop the idea: {out}"
    row = status(case, dropped)
    assert row["status"] == "cleared", f"An idea the PM drops is logged as cleared: {row}"
    assert any(reason in value for value in values(row)), f"The cleared idea's log row keeps the reason: {row}"
    assert status(case, kept)["status"] == "waiting", "Dropping one idea must not clear another"
    assert brief_ideas(case)["count"] == 1, "The dropped idea leaves the ideas place"


def test_a_drop_without_a_reason_is_refused_and_the_idea_keeps_waiting(world):
    case = world
    ident = note(case, "idea", "Opaque idea without a reason to drop")
    for argv in (("order", "drop", ident), ("order", "drop", ident, "--reason", "   ")):
        code, out = run(case, *argv)
        assert code != 0, f"A drop needs a reason: {argv} succeeded: {out}"
        assert status(case, ident)["status"] == "waiting", f"A refused drop leaves the idea waiting: {argv}"
    assert brief_ideas(case)["count"] == 1
    code, out = run(case, "order", "drop", ident, "--reason", "Opaque real reason")
    assert code == 0, f"The same idea drops once a reason is given: {out}"
    assert brief_ideas(case)["count"] == 0
