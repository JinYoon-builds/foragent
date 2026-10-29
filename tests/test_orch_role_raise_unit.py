"""Edges of role-signal raises, answers, scorecard counts and lane_concentration (#794)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_facts_kinds, orch_inbox, orch_role_raise, orch_roles, orch_scorecard
from foragent.orch_facts_resources import _jsonl
from foragent.state import StateStore

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
SIGNAL = {"paths": [], "kinds": ["hot_overlap_files"]}


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


@pytest.fixture
def measured(monkeypatch, tmp_path):
    """One role with one kinds signal; ``state["kinds"]`` decides whether it is met."""
    state = {"kinds": set(), "signals": [dict(SIGNAL)]}
    (tmp_path / ".foragent.yml").write_text("repo: owner/repo\n", encoding="utf-8")
    monkeypatch.setattr(orch_role_raise, "load_config", lambda root: SimpleNamespace())
    monkeypatch.setattr(orch_role_raise, "load_roles", lambda config: {"security": {"signals": state["signals"]}})
    monkeypatch.setattr(orch_roles, "observe", lambda store, config, root, roles: (set(), state["kinds"], state.get("numbers", {})))
    return state


def clears(store):
    return [event for event in _jsonl(store.root / "orch-events.jsonl") if event.get("kind") == "role-signal-clear"]


def test_a_clear_is_recorded_once_per_raise_and_only_after_one(store, tmp_path, measured):
    assert orch_role_raise.raise_due(store, tmp_path, NOW) == []
    assert clears(store) == [], "An unmet signal never raised has nothing to clear"
    measured["kinds"] = {"hot_overlap_files"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    measured["kinds"] = set()
    for minutes in (5, 10):
        store.clock[0] = NOW + timedelta(minutes=minutes)
        orch_role_raise.raise_due(store, tmp_path, store.clock[0])
    assert [(event["role"], event["signal"]) for event in clears(store)] == [("security", SIGNAL)]
    measured["kinds"] = {"hot_overlap_files"}
    store.clock[0] = NOW + timedelta(minutes=15)
    assert orch_role_raise.raise_due(store, tmp_path, store.clock[0]) == [], "Cleared but still open: stay quiet"
    assert orch_inbox.requests(store)[0]["id"] == first["id"]


def test_a_removed_signal_is_never_raised_again(store, tmp_path, measured):
    measured["kinds"] = {"hot_overlap_files"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    orch_role_raise.answer(store, first["id"], "not-now", "signal removed")
    measured["signals"].clear()
    store.clock[0] = NOW + timedelta(days=2)
    assert orch_role_raise.raise_due(store, tmp_path, store.clock[0]) == []


def test_cooldown_reads_the_one_key_and_falls_back_to_12_hours(tmp_path):
    settings = tmp_path / ".foragent.yml"
    assert orch_role_raise.cooldown(tmp_path) == timedelta(hours=12)
    settings.write_text("role_signal_cooldown_hours: 3\n", encoding="utf-8")
    assert orch_role_raise.cooldown(tmp_path) == timedelta(hours=3)
    for bad in ("-1", "soon", "true", "[1]"):
        settings.write_text(f"role_signal_cooldown_hours: {bad}\n", encoding="utf-8")
        assert orch_role_raise.cooldown(tmp_path) == timedelta(hours=12), bad


def test_the_cooldown_key_is_a_valid_settings_key(tmp_path):
    from foragent.config import _parse_yaml

    assert _parse_yaml("role_signal_cooldown_hours: 2\n", tmp_path / ".foragent.yml") == {
        "role_signal_cooldown_hours": 2}


def test_answers_reject_unknown_done_and_non_signal_requests(store, tmp_path, measured):
    plain = orch_inbox.add_request(store, "gate", "proposal", {"role": "security"})
    with pytest.raises(orch_inbox.InboxError, match="not a role signal"):
        orch_role_raise.answer(store, plain["id"], "called", "")
    with pytest.raises(orch_inbox.InboxError, match="no request"):
        orch_role_raise.answer(store, "r99", "called", "")
    measured["kinds"] = {"hot_overlap_files"}
    [raised] = orch_role_raise.raise_due(store, tmp_path, NOW)
    with pytest.raises(orch_inbox.InboxError, match="needs a reason"):
        orch_role_raise.answer(store, raised["id"], "not-now", "   ")
    event = orch_role_raise.answer(store, raised["id"], "called", "")
    assert (event["request_id"], event["role"], event["answer"]) == (raised["id"], "security", "called")
    with pytest.raises(orch_inbox.InboxError, match="already done"):
        orch_role_raise.answer(store, raised["id"], "not-now", "again")
    answers = [row for row in _jsonl(store.root / "orch-events.jsonl") if row.get("kind") == "role-signal-answer"]
    assert len(answers) == 1


def test_a_failing_raise_is_logged_in_one_line_and_the_tick_goes_on(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orch_role_raise, "raise_due", lambda *args: 1 / 0)
    assert orch_role_raise.tick(store, tmp_path, NOW, "en") == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "role_signals" in err[0] and "ZeroDivisionError" in err[0]


def test_previous_window_takes_the_per_day_median_and_human_line_shows_signals(store, tmp_path, measured):
    measured["signals"][:] = [{"paths": [], "kinds": [f"k{n}"]} for n in range(3)]
    measured["kinds"] = {"k0", "k1", "k2"}
    for day, count in ((3, 0), (2, 2), (1, 2)):  # Oldest first.
        store.clock[0] = NOW - timedelta(days=day, hours=1)
        rows = orch_role_raise.raise_due(store, tmp_path, store.clock[0])[:count]
        for row in rows:
            orch_role_raise.answer(store, row["id"], "not-now", "later")
        for row in orch_role_raise.signal_requests(store):
            if row["status"] != "done":
                orch_inbox.set_request_status(store, row["id"], "done")  # Done without an answer event.
    store.clock[0] = NOW
    [today] = orch_role_raise.raise_due(store, tmp_path, NOW)[:1]
    orch_role_raise.answer(store, today["id"], "called", "looked")
    config = SimpleNamespace(roles={"security": {"agent": "fa-security"}})
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=4))
    roles = card["window"]
    assert roles["today"]["roles"]["security"]["signals"] == {"raised": 3, "called": 1, "not_now": 0,
                                                              "unanswered": 2}
    assert roles["previous"]["roles"]["security"]["signals"] == {"raised": 3, "called": 0, "not_now": 2,
                                                                 "unanswered": 1}
    lines = orch_roles.scorecard_lines(roles["today"], roles["previous"], "en")
    assert [line.split(":")[0] for line in lines] == ["Role security"], "All-zero roles get no line"
    assert lines[0].endswith("signals 3 raised · 1 called · 0 not now · 2 unanswered "
                             "(before signals 3 raised · 0 called · 2 not now · 1 unanswered)")


def test_a_role_with_calls_but_no_signals_keeps_its_line_without_signal_numbers():
    zero = {"raised": 0, "called": 0, "not_now": 0, "unanswered": 0}
    today = {"roles": {"docs": {"calls": 1, "accepted_proposals": 0, "signals": zero},
                       "idle": {"calls": 0, "accepted_proposals": 0, "signals": zero}}}
    previous = {"roles": {"docs": {"calls": 0, "accepted_proposals": 0, "signals": zero},
                          "idle": {"calls": 0, "accepted_proposals": 0, "signals": zero}}}
    assert orch_roles.scorecard_lines(today, previous, "ko") == [
        "Role docs: calls 1 (before 0), accepted proposals 0 (before 0)"]
    previous["roles"]["idle"]["signals"] = {**zero, "raised": 1, "unanswered": 1}
    [_, idle] = orch_roles.scorecard_lines(today, previous, "ko")
    assert idle.endswith("(그 전 신호 1건 · 부름 0건 · 나중에 0건 · 답 없음 1건)"), "Either window shows signals"


def test_identical_signals_in_one_tick_raise_once_and_clear_once(store, tmp_path, measured):
    measured["signals"][:] = [dict(SIGNAL), dict(SIGNAL)]
    measured["kinds"] = {"hot_overlap_files"}
    assert len(orch_role_raise.raise_due(store, tmp_path, NOW)) == 1, "The second copy sees the open request"
    assert len(orch_role_raise.signal_requests(store)) == 1
    measured["kinds"] = set()
    store.clock[0] = NOW + timedelta(minutes=5)
    orch_role_raise.raise_due(store, tmp_path, store.clock[0])
    assert len(clears(store)) == 1, "The second copy sees this tick's clear"


def test_a_kind_request_carries_the_facts_numbers_of_that_kind(store, tmp_path, measured):
    measured["kinds"] = {"hot_overlap_files"}
    measured["numbers"] = {"hot_overlap_files": {"runs_by_file": {"src/foragent/state.py": 4}}}
    [row] = orch_role_raise.raise_due(store, tmp_path, NOW)
    assert 'fact hot_overlap_files: {"runs_by_file": {"src/foragent/state.py": 4}}' in row["text"]


def test_a_kind_without_facts_numbers_is_named_alone(store, tmp_path, measured):
    measured["kinds"] = {"hot_overlap_files"}
    [row] = orch_role_raise.raise_due(store, tmp_path, NOW)
    assert "fact hot_overlap_files)" in row["text"]


def test_facts_kinds_give_each_present_kind_its_numbers(store, tmp_path):
    runs(store, ["orch"] * 6 + ["view"] * 2)
    module = tmp_path / "src/pkg/big.py"
    module.parent.mkdir(parents=True)
    module.write_text("x = 1\n" * (orch_facts_kinds.LINE_LIMIT + 2))
    result = orch_facts_kinds.collect(store, LANES, tmp_path, NOW, [], [], [])
    assert result["kinds"] == ["files_over_limit", "lane_concentration"]
    assert result["kind_numbers"] == {
        "files_over_limit": {"limit": orch_facts_kinds.LINE_LIMIT,
                             "lines_by_file": {"src/pkg/big.py": orch_facts_kinds.LINE_LIMIT + 2}},
        "lane_concentration": {"runs": 8, "lane": "orch", "lane_runs": 6, "idle_lanes": ["board", "main"]},
    }


def runs(store, lanes, *, hours_ago=1.0, finish=True):
    for index, lane in enumerate(lanes):
        store.clock[0] = NOW - timedelta(hours=hours_ago, minutes=index)
        number = 100 + index
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane=lane)
        if finish:
            store.finish_run(number, outcome="checks-failed")
    store.clock[0] = NOW


LANES = SimpleNamespace(lanes={"orch": {}, "view": {}, "board": {}, "architect": {}, "docs": {}})


@pytest.mark.parametrize("lanes,expected", (
    (["orch"] * 5 + ["view"] * 3, True),
    (["orch"] * 4 + ["view"] * 3, False),
    (["orch"] * 4 + ["view"] * 4, False),
    (["orch"] * 5 + ["view", "board", "main"], False),
    (["orch"] * 6 + ["view"] * 2, True),
), ids=("concentrated", "under-8-runs", "half-is-not-more", "roles-are-not-lanes", "main-counts-as-idle"))
def test_lane_concentration_needs_8_runs_a_majority_lane_and_two_idle_lanes(store, lanes, expected):
    runs(store, lanes)
    assert orch_facts_kinds.lane_concentration(store, LANES, NOW, []) is expected


def test_lane_concentration_counts_retries_and_running_runs_but_not_older_starts(store):
    runs(store, ["orch"] * 4 + ["view"] * 3)
    runs(store, ["orch"] * 8, hours_ago=7)
    assert orch_facts_kinds.lane_concentration(store, LANES, NOW, []) is False, "Only starts in the last 6 hours"
    store.clock[0] = NOW - timedelta(minutes=30)
    run = store.start_run(100, branch="foragent/issue-100", worktree=store.worktree_path(100), lane="orch")
    store.clock[0] = NOW
    assert orch_facts_kinds.lane_concentration(store, LANES, NOW, [run]) is True, (
        "A retry of #100 still running is its own started execution")


def test_a_folder_without_settings_raises_nothing_and_logs_nothing(store, tmp_path, capsys):
    assert orch_role_raise.tick(store, tmp_path, NOW, "en") == []
    assert capsys.readouterr().err == "", "No .foragent.yml is no roles source, not a failure"
    assert orch_inbox.requests(store) == []
