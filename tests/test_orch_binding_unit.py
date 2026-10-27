"""The tick's binding events and the scorecard's binding line (D96)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from foragent import orch_binding, orch_inbox, orch_scorecard
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def _store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def _lines(store):
    path = store.root / orch_binding.FILE
    return path.read_text().splitlines() if path.exists() else []


def test_record_appends_once_per_change(tmp_path):
    store = _store(tmp_path)
    assert orch_binding.record(store, NOW, "test_slots") is True
    assert orch_binding.record(store, NOW + timedelta(minutes=5), "test_slots") is False
    assert orch_binding.record(store, NOW + timedelta(minutes=10), "structure") is True
    assert len(_lines(store)) == 2
    assert orch_binding.at(store, NOW + timedelta(minutes=7)) == "test_slots"
    assert orch_binding.at(store, NOW - timedelta(minutes=1)) is None
    assert not (store.root / "orch-events.jsonl").exists(), "The action journal stays free of binding events"


def test_from_tick_reads_held_work_without_probes(tmp_path):
    store, config = _store(tmp_path), Config()
    assert orch_binding.from_tick(store, config, NOW, ("normal", 0.1), False) == "none"
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    store.update_run(5, phase="test-slot-wait", phase_started_at=NOW.isoformat())
    assert orch_binding.from_tick(store, config, NOW, ("normal", 0.1), False) == "test_slots"
    assert orch_binding.from_tick(store, config, NOW, ("normal", 0.1), True) == "github"
    assert orch_binding.from_tick(store, config, NOW, ("critical", 0.1), False) == "machine"


def test_record_tick_uses_the_tick_context(tmp_path):
    store = _store(tmp_path)
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: NOW, _budget_sample=("normal", 0.1))
    assert orch_binding.record_tick(ctx) is True and orch_binding.record_tick(ctx) is False
    assert len(_lines(store)) == 1


def test_scorecard_binding_line_says_when_it_changed_from_the_previous_day(tmp_path):
    store = _store(tmp_path)
    orch_binding.record(store, NOW - timedelta(days=1, hours=2), "structure")
    orch_binding.record(store, NOW - timedelta(hours=3), "test_slots")
    card = orch_scorecard.collect(store, 2)
    card["binding"] = orch_scorecard.binding(store, Config(), tmp_path, card)
    assert card["binding"] == {"today": "test_slots", "previous": "structure"}
    shown = orch_scorecard.human(card, "en")
    assert "Binding constraint now: test_slots (changed from structure the previous day)" in shown
    orch_binding.record(store, NOW - timedelta(hours=1), "structure")
    card["binding"] = orch_scorecard.binding(store, Config(), tmp_path, card)
    assert "changed" not in orch_scorecard.human(card, "en").split("Binding")[1]


def test_worst_skips_flow_metrics(tmp_path):
    card = {"worse": ["idle_minutes", "wait_minutes_by_cause.slot", "spec_questions"],
            "window": {"today": {"spec_questions": 2, "idle_minutes": 50},
                       "previous": {"spec_questions": 1, "idle_minutes": 1}}}
    assert orch_scorecard.worst(card) == "spec_questions"
    card["worse"] = ["idle_minutes"]
    assert orch_scorecard.worst(card) is None


def test_person_found_appends_one_event_and_rejects_empty_text(tmp_path):
    store = _store(tmp_path)
    event = orch_inbox.person_found(store, "Queue stalled")
    assert event["kind"] == "person-found" and event["at"] == NOW.isoformat()
    try:
        orch_inbox.person_found(store, "  ")
    except orch_inbox.InboxError:
        pass
    else:
        raise AssertionError("Empty text must be refused")
    assert len((store.root / "orch-events.jsonl").read_text().splitlines()) == 1
