"""Unit checks for the request box and decisions places (#2333) that the acceptance file leaves loose."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

from foragent import orch_inbox, shelf_decisions
from foragent.state import StateStore

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
LEDGER = "# Decisions\n\n" + "".join(f"### D{n}. 2026-09-0{n} Decision {n}\nText {n}.\n\n" for n in range(1, 5))


def _store(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


def test_idle_capacity_takes_only_as_many_decisions_as_free_slots(tmp_path):
    clock = [NOW]
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/decisions.md").write_text(LEDGER, encoding="utf-8")
    store = _store(tmp_path, clock)
    ctx = SimpleNamespace(store=store, repo_root=tmp_path, config=SimpleNamespace(target_concurrency=2))
    request = shelf_decisions.tick(ctx)
    assert request["decisions"] == ["D1", "D2"], "Two free slots take the two oldest decisions only"
    plans = sorted(path.parent.name for path in (store.root / "drafts").glob("*/plan.json"))
    assert plans == ["decision-d1", "decision-d2"]
    assert json.loads((store.root / "drafts/decision-d1/plan.json").read_text())["decisions"] == ["D1"]
    assert shelf_decisions.tick(ctx) is None, "An open drafting request is not filed again"


def test_person_decisions_respect_the_rolling_daily_cap(tmp_path):
    clock = [NOW]
    store = _store(tmp_path, clock)
    for n in range(6):
        orch_inbox.add_request(store, "pm", f"Pick {n}\n{orch_inbox.PERSON_MARK}\n")
    assert len(orch_inbox.deliver_person_decisions(store)) == 5
    assert orch_inbox.deliver_person_decisions(store) == []
    clock[0] += timedelta(hours=23)
    assert orch_inbox.deliver_person_decisions(store) == [], "The cap counts the last 24 hours"
    clock[0] += timedelta(hours=2)
    assert len(orch_inbox.deliver_person_decisions(store)) == 1
    assert len(orch_inbox.questions(store)) == 6, "No request is asked twice"


def test_marked_questions_from_any_path_count_toward_the_person_cap(tmp_path):
    clock = [NOW]
    store = _store(tmp_path, clock)
    for n in range(5):
        orch_inbox.ask(store, f"Asked directly {n}\n{orch_inbox.PERSON_MARK}", ["a", "b"], 1)
    orch_inbox.ask(store, "An unmarked question", ["a", "b"], 1)
    orch_inbox.add_request(store, "pm", f"Pick\n{orch_inbox.PERSON_MARK}")
    assert orch_inbox.deliver_person_decisions(store) == [], "Five marked questions today fill the cap"
    assert orch_inbox.ask_once(store, "other-path", f"Also marked\n{orch_inbox.PERSON_MARK}", ["a", "b"], 1) is None
    clock[0] += timedelta(hours=24, minutes=1)
    assert len(orch_inbox.deliver_person_decisions(store)) == 1


def test_a_direct_ask_after_five_delivered_decisions_is_refused(tmp_path):
    clock = [NOW]
    store = _store(tmp_path, clock)
    for n in range(5):
        orch_inbox.add_request(store, "pm", f"Pick {n}\n{orch_inbox.PERSON_MARK}")
    assert len(orch_inbox.deliver_person_decisions(store)) == 5
    try:
        orch_inbox.ask(store, f"One more\n{orch_inbox.PERSON_MARK}", ["a", "b"], 1)
    except orch_inbox.PersonCapFull:
        pass
    else:
        raise AssertionError("A sixth marked question in 24 hours must be refused")
    assert len(orch_inbox.questions(store)) == 5
    orch_inbox.ask(store, "An unmarked question still goes in", ["a", "b"], 1)
    assert len(orch_inbox.questions(store)) == 6
