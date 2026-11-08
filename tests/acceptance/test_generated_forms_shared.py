"""Acceptance tests for #2868 (r1291 redesign): generated body parts are described once.

Issue #7 went through test preparation and is in progress. The edit is taken from ``fa order edit 7
--json`` and only carries a corrected locked file (the lock-correction world of
``lock_corrections_beside_case.py``, prepared as in ``restate_generated_case.py``). Bodies come from
``generated_forms_case.py``.

4. The lock-only edit is accepted when the body carries the markers publication and the gate add after
   the plan-child marker: owner-label, waiting (including a '## Waiting' section the gate writes),
   decision, removal (done-when-removed) and write markers.
5. (test_preparation_notes_single_line.py) Publication writes each preparation note as a single line.
6. A section a person added anywhere in the body, including between the preparation notes and the
   plan-child marker, makes the edit need reviewed tests, and publication never deletes it.
"""

import pytest

from lock_corrections_beside_case import (CLOSED, HELPER, LOCKED, LOCKED_TEST, NEW_CLOSED, NEW_HELPER,  # noqa: F401
                                          NUMBER, OLD_CLOSED, OLD_HELPER, BRANCH, accepted, apply, case,
                                          commits_touching, describe, offline_publication, on_origin,
                                          publish_and_check, world)
from restate_generated_case import correction_from_seed, nothing_prepared, seed  # noqa: F401
from generated_forms_case import (FORMS, NOTES, PLACES, WAITING_TEXT, generated_body, generated_only,
                                  hand_added_body, hand_text, set_body)
from foragent import order_publications, orch_inbox
from foragent.scheduler import TickReport

REVIEWED_REFUSAL = "no reviewed tests"


def _stands_approved(world):
    labels = set(world.tracker.get_issue(NUMBER).labels)
    assert "approved" in labels, f"Issue #{NUMBER} must stand approved; labels {labels}"
    assert "needs-human" not in labels, f"The correction must not wait on a person; labels {labels}"
    assert orch_inbox.questions(world.case.store) == [], "No question for the PM or a person"


def _forms_are_what_publication_and_the_gate_write(world):
    """Setup check: each form carries the generated part it names (not a standalone test)."""
    every = generated_body(world.body, "all")
    assert every.startswith(world.body + "\n## Preparation notes\n" + "".join(f"- {note}\n" for note in NOTES)
                            + "\n<!-- foragent:plan-child "), "notes, then the plan-child marker"
    after = every.partition("<!-- foragent:plan-child ")[2]
    for marker in ("<!-- foragent:owner-label -->", "<!-- foragent:decisions ",
                   "<!-- foragent:waiting-prior ", "<!-- foragent:write ", "<!-- foragent:done-when-removed -->"):
        assert marker in after, f"{marker} sits after the plan-child marker"
    assert "## Waiting\n" + WAITING_TEXT in after, "The gate's waiting section sits after the plan-child marker"
    assert "## Waiting" not in generated_body(world.body, "waiting"), "The park was undone; only its marker stays"


# Statement 4: every generated form is accepted as generated ----------------------------------


@pytest.mark.parametrize("form", FORMS)
def test_lock_only_edit_publishes_whatever_the_generated_form(world, monkeypatch, capsys, form):
    _forms_are_what_publication_and_the_gate_write(world)
    body = set_body(world, generated_body(world.body, form))
    seeded = seed(world, monkeypatch, capsys)
    started = nothing_prepared(monkeypatch)
    plan = correction_from_seed(seeded, {HELPER: NEW_HELPER}, plan_id=f"2026-11-09-widget-{form.replace('_', '-')}")
    row = publish_and_check(world, plan)
    assert world.refusal is None or REVIEWED_REFUSAL not in world.refusal, (
        f"A lock-only edit of a body whose generated part has the {form} form must not need reviewed tests; "
        f"got {describe(world, row)}\nbody:\n{body}")
    assert accepted(row), (
        f"The lock-only edit of a body with the {form} form must pass the publication check; "
        f"got {describe(world, row)}")
    row = apply(world, plan)
    assert row["status"] == "published", describe(world, row)
    assert on_origin(world, "main", HELPER) == NEW_HELPER, "The corrected helper must land on main"
    assert on_origin(world, "main", LOCKED) == LOCKED_TEST and on_origin(world, "main", CLOSED) == OLD_CLOSED, (
        "Only the helper changes on main")
    assert started == [], "No test preparation runs for a lock-only correction"
    _stands_approved(world)


def test_closed_lock_correction_with_every_generated_form_lands_in_the_issue_pr(world, monkeypatch, capsys):
    set_body(world, generated_body(world.body, "all"))
    started = nothing_prepared(monkeypatch)
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {CLOSED: NEW_CLOSED},
                                plan_id="2026-11-09-widget-label-all")
    row = publish_and_check(world, plan)
    assert world.refusal is None or REVIEWED_REFUSAL not in world.refusal, describe(world, row)
    assert row is not None and row["status"] != "refused", describe(world, row)
    apply(world, plan)
    assert on_origin(world, "main", CLOSED) == OLD_CLOSED, "A main-red correction must not land on main"
    assert on_origin(world, BRANCH, CLOSED) == NEW_CLOSED, f"It lands in #7's PR (branch {BRANCH})"
    assert started == []
    _stands_approved(world)


def test_lock_only_edit_with_every_generated_form_lands_once(world, monkeypatch, capsys):
    """Doubled effect: a second apply pass lands nothing more on main."""
    set_body(world, generated_body(world.body, "all"))
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {HELPER: NEW_HELPER},
                                plan_id="2026-11-09-widget-all-once")
    row = publish_and_check(world, plan)
    assert accepted(row), describe(world, row)
    apply(world, plan)
    apply(world, plan)
    assert on_origin(world, "main", HELPER) == NEW_HELPER
    assert commits_touching(world, "main", HELPER, since=world.main) == 1, (
        "The corrected helper lands on main in exactly one commit")


@pytest.mark.parametrize("form", ["waiting_section", "all"])
def test_authored_change_with_a_generated_form_still_needs_reviewed_tests(world, monkeypatch, capsys, form):
    """Control: the shared description of the generated part does not let an authored change through."""
    body = set_body(world, generated_body(world.body, form))
    seeded = seed(world, monkeypatch, capsys)
    changed = correction_from_seed({**seeded, "goal": seeded["goal"] + " Gears count twice."},
                                   {HELPER: NEW_HELPER}, plan_id=f"2026-11-09-widget-goal-{form.replace('_', '-')}")
    row = publish_and_check(world, changed)
    assert row is None and world.refusal is not None and REVIEWED_REFUSAL in world.refusal, (
        f"A goal change still needs reviewed tests; got {describe(world, row)}")
    assert world.tracker.get_issue(NUMBER).body == body
    restated = correction_from_seed(seeded, {HELPER: NEW_HELPER}, plan_id=f"2026-11-09-widget-re-{form.replace('_', '-')}")
    assert accepted(publish_and_check(world, restated)), describe(world, None)


# Statement 6: a hand-added section needs reviewed tests and is never deleted -----------------


@pytest.mark.parametrize("place", PLACES)
def test_hand_added_section_needs_reviewed_tests_and_is_kept(world, monkeypatch, capsys, place):
    body = set_body(world, hand_added_body(world.body, place))
    added = hand_text(place)
    seeded = seed(world, monkeypatch, capsys)
    started = nothing_prepared(monkeypatch)
    for approve in (True, False):
        plan = correction_from_seed(seeded, {HELPER: NEW_HELPER},
                                    plan_id=f"2026-11-09-hand-{place.replace('_', '-')}-{int(approve)}")
        row = publish_and_check(world, plan, approve=approve)
        assert row is None and world.refusal is not None and REVIEWED_REFUSAL in world.refusal, (
            f"A section a person added ({place}) is an authored change: the edit needs reviewed tests "
            f"(approve={approve}); got {describe(world, row)}")
        order_publications.apply_next(world.case.ctx, TickReport())
        current = world.tracker.get_issue(NUMBER).body
        assert added in current, f"Publication must never delete the section a person added ({place})"
        assert current == body, "A refused edit writes nothing on the issue"
        assert on_origin(world, "main", HELPER) == OLD_HELPER, "A refused edit lands nothing"
    assert started == [], "The refusal comes from publication, not from starting preparation"
    # Contrast: once the person's section is gone again, the same restate is a lock-only correction.
    set_body(world, generated_only(world.body))
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {HELPER: NEW_HELPER},
                                plan_id=f"2026-11-09-hand-gone-{place.replace('_', '-')}")
    row = publish_and_check(world, plan)
    assert accepted(row), (
        f"Without the hand-added section the body is generated-only and the restate is lock-only; "
        f"got {describe(world, row)}\nbody:\n{world.tracker.get_issue(NUMBER).body}")


def test_hand_added_section_between_notes_and_marker_is_kept_with_every_generated_form(world, monkeypatch, capsys):
    """The r1291 reproduction on a body carrying every generated form at once."""
    every = generated_body(world.body, "all")
    head, marker, tail = every.partition("\n<!-- foragent:plan-child ")
    assert marker, every
    added = "## Rollout\nShip the widget count on Friday, after the gear audit.\n"
    body = set_body(world, head + "\n" + added + marker + tail)
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {HELPER: NEW_HELPER},
                                plan_id="2026-11-09-hand-all")
    row = publish_and_check(world, plan)
    assert row is None and world.refusal is not None and REVIEWED_REFUSAL in world.refusal, describe(world, row)
    order_publications.apply_next(world.case.ctx, TickReport())
    assert world.tracker.get_issue(NUMBER).body == body, "Publication never deletes the person's section"
    set_body(world, every)
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {HELPER: NEW_HELPER},
                                plan_id="2026-11-09-hand-all-gone")
    assert accepted(publish_and_check(world, plan)), describe(world, None)
