"""Acceptance test for #2868 (architect r1293): publication writes each preparation note as one line.

Offline: the writer and reviewer are the shared fake executors of ``lane_tests_case``; their replies carry
notes. The order is prepared with ``fa order prepare`` and published with ``fa order publish --approve``.
The published child's ``## Preparation notes`` section holds one ``- `` line per note: the note's own line
breaks (including blank lines) and runs of whitespace are folded into single spaces. No real agent,
GitHub call, signal, commit or push.
"""

from dataclasses import replace
import json

import pytest

from foragent import executors
from lane_tests_case import TEST_PATH, lane_case, offline_publication, prepare, publish  # noqa: F401

WRITER_NOTES = ["The writer kept the widget fixture\nin widget_support.py.",
                "First paragraph of a long note.\n\nSecond   paragraph,\twith a tab.\r\n  Third line, indented."]
REVIEWER_NOTES = ["The reviewer measured the locked test red on main."]
FOLDED = ["The writer kept the widget fixture in widget_support.py.",
          "First paragraph of a long note. Second paragraph, with a tab. Third line, indented.",
          "The reviewer measured the locked test red on main."]


def _with_notes(monkeypatch):
    for cls, notes in ((executors.CodexExecutor, WRITER_NOTES), (executors.ClaudeExecutor, REVIEWER_NOTES)):
        run = cls.run

        def with_notes(self, _run=run, _notes=notes, **kwargs):
            result = _run(self, **kwargs)
            reply = json.loads(result.last_message) | {"notes": _notes}
            return replace(result, last_message=json.dumps(reply))

        monkeypatch.setattr(cls, "run", with_notes)


def _notes_section(body):
    head, heading, rest = body.partition("\n## Preparation notes\n")
    assert heading, f"The published child carries a Preparation notes section:\n{body}"
    assert "## Preparation notes" not in rest, f"One Preparation notes section, written once:\n{body}"
    section = rest.split("\n\n", 1)[0]
    return section.split("\n")


@pytest.fixture
def published(lane_case, monkeypatch, capsys):  # noqa: F811
    _with_notes(monkeypatch)
    row = prepare(lane_case, capsys)
    assert row["status"] == "reviewed", row
    code, out, err = publish(lane_case, capsys, "--approve")
    assert code == 0, out + err
    children = [issue for issue in lane_case.tracker.issues.values() if issue.title == "Notice delivery"]
    assert len(children) == 1, [issue.title for issue in lane_case.tracker.issues.values()]
    # Setup check: folding the notes changes nothing else of the publication.
    assert TEST_PATH in children[0].body, children[0].body
    return children[0].body


def test_each_preparation_note_is_published_as_one_line(published):
    lines = _notes_section(published)
    assert lines == [f"- {note}" for note in FOLDED], (
        "Each preparation note is one '- ' line, its line breaks and whitespace runs folded into single spaces; "
        f"got {lines!r}\nbody:\n{published}")


def test_no_line_of_a_multi_line_note_leaks_outside_its_note_line(published):
    """Skipped folding leaves the note's continuation lines (or a blank line) in the body."""
    for fragment in ("\nin widget_support.py.", "\nSecond", "\n  Third", "\nThird", "   paragraph", "\t", "\r"):
        assert fragment not in published, f"{fragment!r} must be folded into its note's line:\n{published}"
    for note in FOLDED:
        assert published.count(note) == 1, f"Each note is written exactly once: {note!r}\n{published}"

