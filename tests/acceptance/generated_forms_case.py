"""Bodies of a prepared issue in every generated form, and with hand-added sections (helper, not a test file).

The world is ``restate_generated_case.py`` (issue #7, published on the statement route, in progress on
``foragent/issue-7``). Its body is rebuilt here the way publication and the gate write it, using the
product's own writers for each generated part:

- the ``## Preparation notes`` section exactly as publish renders it (``- {note}\\n`` per note, one line
  each: architect r1293, real bodies carry no multi-line notes);
- the plan-child marker and the owner-label contract line publish appends to a child;
- the Done when removal record (``order_done_when.with_record``) after the plan-child marker, where
  the census of real bodies (r1293) found it;
- the decision marker (``decisions.with_marker``);
- the waiting marker the gate leaves after a park and unpark, and the ``## Waiting`` section it writes
  while the issue is parked (``order_waiting.replace_section``, ``stamp`` and ``mark_write``, as
  ``orch_park`` calls them);
- the write marker (``order_waiting.mark_write``).

A hand-added section is text a person typed into the body: a ``## `` heading with its text, a plain
paragraph, or a line continuing a preparation note, at some position between or after the parts above.
"""

from foragent import decisions, order_waiting
from foragent import order_done_when
from foragent.ownership import OWNED_MARKER

from lock_corrections_beside_case import NUMBER
from restate_generated_case import PUBLISHED_KEY, PUBLISHED_ORDER

ONE_LINE_NOTES = ["The writer kept the widget fixture in widget_support.py."]
NOTES = ["The writer kept the widget fixture in widget_support.py.",
         "The reviewer measured the locked test red on main."]
REMOVED_LINE = "uv run pytest -q tests/acceptance/test_widget_retired.py"
DECISIONS = ["D12"]

# Every generated form a real body carries after the plan-child marker (census of r1293).
FORMS = ["owner_label", "waiting", "waiting_section", "decision", "removal", "write", "all"]
WAITING_TEXT = ("Reason: waiting for the gear audit\nNext condition: The orchestrator confirms the reason is "
                "resolved, then explicitly unparks this issue.\nResponsible role: orchestrator")

HAND_SECTION = "## Rollout\nShip the widget count on Friday, after the gear audit.\n"
HAND_PARAGRAPH = "Please also double-check the gear widgets before merging.\n"
HAND_CONTINUATION = "  Also check that gears are never counted as widgets.\n"
# Where a person typed the section into the body.
PLACES = ["between_notes_and_marker", "between_links_and_notes", "after_the_markers", "between_markers",
          "paragraph_after_the_markers", "inside_authored_sections", "note_continuation"]


def _notes(notes):
    return "\n## Preparation notes\n" + "".join(f"- {note}\n" for note in notes)


def _child_marker():
    return f"<!-- foragent:plan-child id={PUBLISHED_ORDER} key={PUBLISHED_KEY} -->"


def _park(body):
    """The body after the gate parked the issue (orch_park._waiting): it carries a ``## Waiting`` section."""
    parked = order_waiting.replace_section(body, WAITING_TEXT)
    return order_waiting.mark_write(body, order_waiting.stamp(body, parked))


def _park_and_unpark(body):
    """The body after the gate parked the issue and unparked it again (orch_park._waiting twice)."""
    parked = _park(body)
    unparked = order_waiting.replace_section(parked, None)
    return order_waiting.mark_write(parked, order_waiting.stamp(parked, unparked))


def generated_body(authored, form):
    """``authored`` (the rendered body with its acceptance links) with the generated part in ``form``."""
    every = form == "all"
    body = authored + _notes(NOTES) + "\n" + _child_marker() + "\n"
    if every or form == "owner_label":
        body += OWNED_MARKER + "\n"
    if every or form == "removal":
        body = order_done_when.with_record(body, None, [{"line": REMOVED_LINE, "reason": "Retired widgets."}],
                                           PUBLISHED_ORDER)
    if every or form == "decision":
        body = decisions.with_marker(body, DECISIONS, None)
    if every or form == "waiting":
        body = _park_and_unpark(body)
    if every or form == "waiting_section":
        body = _park(body)
    if every or form == "write":
        body = order_waiting.mark_write(authored, body)
    return body


def hand_added_body(authored, place):
    """A generated-only body (one-line notes, plan-child and owner markers) with a person's section."""
    notes = _notes(ONE_LINE_NOTES)
    marker = _child_marker() + "\n"
    owner = OWNED_MARKER + "\n"
    if place == "between_notes_and_marker":
        return authored + notes + "\n" + HAND_SECTION + "\n" + marker + owner
    if place == "between_links_and_notes":
        return authored + "\n" + HAND_SECTION + notes + "\n" + marker + owner
    if place == "after_the_markers":
        return authored + notes + "\n" + marker + owner + "\n" + HAND_SECTION
    if place == "between_markers":
        return authored + notes + "\n" + marker + "\n" + HAND_SECTION + "\n" + owner
    if place == "paragraph_after_the_markers":
        return authored + notes + "\n" + marker + owner + "\n" + HAND_PARAGRAPH
    if place == "note_continuation":
        return authored + notes.rstrip("\n") + "\n" + HAND_CONTINUATION + "\n" + marker + owner
    assert place == "inside_authored_sections", place
    head, heading, rest = authored.partition("## Behaviour")
    assert heading, authored
    return head + HAND_SECTION + "\n" + heading + rest + notes + "\n" + marker + owner


def generated_only(authored):
    """The body ``hand_added_body`` starts from: one-line notes, plan-child and owner markers."""
    return authored + _notes(ONE_LINE_NOTES) + "\n" + _child_marker() + "\n" + OWNED_MARKER + "\n"


def hand_text(place):
    return {"paragraph_after_the_markers": HAND_PARAGRAPH,
            "note_continuation": HAND_CONTINUATION}.get(place, HAND_SECTION)


def set_body(world, body):
    """Write ``body`` on #7 as a person or the gate would, and confirm it reads back exactly."""
    issue = world.tracker.get_issue(NUMBER)
    world.tracker.update_issue(NUMBER, title=issue.title, body=body)
    assert world.tracker.get_issue(NUMBER).body == body
    world.prepared_body = body
    return body
