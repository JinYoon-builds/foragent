"""Boundaries of the orchestrator's person line (--say) in the journal and the view."""

from datetime import UTC, datetime
import json

import pytest

from foragent.orch_events import append_event
from foragent.watch_sections.activity import SAY_KINDS, person_line
from foragent.state import StateStore

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def journal(store: StateStore) -> list[dict]:
    return [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("say", [None, "", "  \n "])
def test_missing_or_blank_say_writes_no_say_key(tmp_path, say):
    store = StateStore(tmp_path)
    append_event(store, AT, 3, "retry", "#3: queued implementation retry", say)
    assert "say" not in journal(store)[0]


def test_say_is_normalized_and_kept_beside_the_raw_summary(tmp_path):
    store = StateStore(tmp_path)
    append_event(store, AT, 3, "retry", "#3: queued   implementation retry", "  #3 작업을\n다시 봐요 ")
    event = journal(store)[0]
    assert event["summary"] == "#3: queued implementation retry"
    assert event["say"] == "#3 작업을 다시 봐요"


@pytest.mark.parametrize("language,expected", [("ko", "#7 작업을 다시 맡겼어요"),
                                               ("en", "#7: I handed this back for another try.")])
def test_verb_event_without_say_uses_the_localized_kind_line(language, expected):
    # Journal lines written before --say existed have no say key at all.
    event = {"issue": 7, "kind": "retry", "summary": "#7: queued implementation retry"}
    assert person_line(event, language) == expected


@pytest.mark.parametrize("kind", SAY_KINDS)
def test_every_say_kind_has_a_line_in_both_languages(kind):
    event = {"issue": 2, "kind": kind, "summary": "#2: raw"}
    for language in ("ko", "en"):
        line = person_line(event, language)
        assert "#2" in line and "raw" not in line and "orch_say_" not in line


def test_given_say_wins_over_kind_line():
    event = {"issue": 7, "kind": "escalate", "summary": "raw", "say": "결정해 주세요"}
    assert person_line(event, "ko") == "결정해 주세요"


@pytest.mark.parametrize("event", [
    {"issue": 2, "kind": "retry", "summary": "older line"},  # not written by the orch verb
    {"issue": 2, "kind": "retry", "summary": "#3: older line"},  # a different issue's summary
    {"issue": 2, "kind": "crash-restart", "summary": "#2: older line"},
    {"issue": None, "kind": "retry", "summary": "#2: older line"},
])
def test_other_events_keep_their_raw_summary(event):
    assert person_line(event, "ko") == event["summary"]
