"""Shared world for restate-generated-parts (helper, not a test file).

The lock-correction world of ``lock_corrections_beside_case.py`` (open issue #7 in progress on
``foragent/issue-7``, closed issue #5 green on main, real local Git, scripted measurements), with one
difference: #7 went through test preparation, so its published body ends the way ``fa order publish``
writes a statement-route child: the authored sections and acceptance links, then the generated
``## Preparation notes`` section, then the plan-child marker of the order that published it.

The edit under test is taken from ``fa order edit 7 --json`` (the public seed command), unchanged
except for the corrected locked files it carries.
"""

import json

import pytest

from lock_corrections_beside_case import (CLOSED, HELPER, LOCKED, NUMBER, case, offline_publication,  # noqa: F401
                                          world)
from foragent import cli

# The order that first published #7 on the statement route, and its key for #7.
PUBLISHED_ORDER = "2026-11-01-widget-count"
PUBLISHED_KEY = "widget-count"
NOTES = ["The writer kept the widget fixture in widget_support.py.",
         "The reviewer measured the locked test red on main."]


def generated_parts(order_id=PUBLISHED_ORDER, key=PUBLISHED_KEY, notes=NOTES):
    """The text publish appends after the authored body of a prepared statement-route child."""
    return ("\n## Preparation notes\n" + "".join(f"- {note}\n" for note in notes)
            + f"\n<!-- foragent:plan-child id={order_id} key={key} -->\n")


@pytest.fixture
def prepared(world):  # noqa: F811
    """The world with #7's body as publish wrote it after test preparation."""
    issue = world.tracker.get_issue(NUMBER)
    assert issue.body == world.body
    prepared_body = world.body + generated_parts()
    world.tracker.update_issue(NUMBER, title=issue.title, body=prepared_body)
    assert world.tracker.get_issue(NUMBER).body == prepared_body
    world.prepared_body = prepared_body
    return world


def seed(world, monkeypatch, capsys, number=NUMBER):
    """What ``fa order edit N --json`` prints for the issue, through the CLI."""
    case = world.case
    monkeypatch.setattr(cli, "GhTracker", lambda repo: case.tracker)
    monkeypatch.setattr(cli, "load_config", lambda root: case.config)
    capsys.readouterr()
    code = cli.main(["order", "edit", str(number), "--repo-root", str(world.root), "--json"])
    out = capsys.readouterr().out
    assert code in (0, None), f"fa order edit {number} failed: {out}"
    return json.loads(out)


def correction_from_seed(seeded, files, plan_id="2026-11-09-widget-lock"):
    """An order whose only edit is the seed, carrying only the corrected locked files."""
    edit = {**seeded, "acceptance_files": dict(files)}
    return {"id": plan_id, "title": "Widget lock correction", "summary": "Stale locks",
            "spec_markdown": "# Stale locks\n", "issues": [], "edits": [edit]}


def nothing_prepared(monkeypatch):
    """Record any test preparation (writer, reviewer) started for an edit."""
    from foragent import order_prepare
    started = []

    def refuse(*args, **kwargs):
        started.append("prepare")
        raise AssertionError("test preparation started")

    monkeypatch.setattr(order_prepare, "prepare", refuse)
    return started

