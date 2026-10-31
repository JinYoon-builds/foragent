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
    monkeypatch.setattr(orch_roles, "observe", lambda store, config, root, roles: (set(), state["kinds"], state.get("numbers", {}), state.get("binding")))
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
        measured["numbers"] = {f"k{n}": {"day": day} for n in range(3)}  # New facts re-arm not-now (#1155).
        rows = orch_role_raise.raise_due(store, tmp_path, store.clock[0])[:count]
        for row in rows:
            orch_role_raise.answer(store, row["id"], "not-now", "later")
        for row in orch_role_raise.signal_requests(store):
            if row["status"] != "done":
                orch_inbox.set_request_status(store, row["id"], "done")  # Done without an answer event.
    store.clock[0] = NOW
    measured["numbers"] = {f"k{n}": {"day": 0} for n in range(3)}
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
                             "(before signals 3 raised · 0 called · 2 not now · 1 unanswered), "
                             "2 signal(s) still open")


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


def test_a_removed_role_keeps_its_signals_on_the_scorecard_until_answered(store, tmp_path, measured):
    measured["signals"][:] = [dict(SIGNAL), {"paths": [], "kinds": ["files_over_limit"]}]
    measured["kinds"] = {"hot_overlap_files"}
    store.clock[0] = NOW - timedelta(days=9)  # Outside every window.
    [old] = orch_role_raise.raise_due(store, tmp_path, store.clock[0])
    measured["kinds"] = {"files_over_limit"}
    store.clock[0] = NOW - timedelta(hours=1)
    [recent] = orch_role_raise.raise_due(store, tmp_path, store.clock[0])
    orch_role_raise.answer(store, recent["id"], "called", "looked")
    store.clock[0] = NOW
    config = SimpleNamespace(roles={})  # The security role was removed from the roles source.
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=4))
    row = card["window"]["today"]["roles"]["security"]
    assert row["open_signals"] == 1
    assert row["signals"] == {"raised": 1, "called": 1, "not_now": 0, "unanswered": 0}
    assert "security" in card["window"]["previous"]["roles"]
    [line] = [line for line in orch_roles.scorecard_lines(card["window"]["today"], card["window"]["previous"])
              if line.startswith("Role security")]
    assert line.endswith("1 signal(s) still open")
    store.clock[0] = NOW + timedelta(days=5)  # Only the old, still-open signal is left.
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=4))
    window = card["window"]
    assert window["today"]["roles"]["security"]["signals"]["raised"] == 0
    assert [line for line in orch_roles.scorecard_lines(window["today"], window["previous"])
            if line.startswith("Role security")], "An open signal keeps the role's line"
    orch_role_raise.answer(store, old["id"], "not-now", "role removed")
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=4))
    assert "security" not in card["window"]["today"]["roles"], "Answered and outside the window: gone"


def test_two_concurrent_answers_to_one_signal_record_one_answer(store, tmp_path, measured, monkeypatch):
    """A second answer arrives while the first is checking the request: exactly one succeeds."""
    import threading
    import time

    measured["kinds"] = {"hot_overlap_files"}
    [row] = orch_role_raise.raise_due(store, tmp_path, NOW)
    original, outcomes, threads = orch_role_raise.is_signal_request, {}, []

    def answer(how, why):
        try:
            orch_role_raise.answer(store, row["id"], how, why)
            outcomes[how] = "ok"
        except orch_inbox.InboxError as exc:
            outcomes[how] = str(exc)

    def checking(record):
        if not threads:  # The first answer is checking: start the competing one now.
            threads.append(threading.Thread(target=answer, args=("not-now", "racing")))
            threads[0].start()
            time.sleep(0.3)  # Give it time to check and wait, before the first one writes.
        return original(record)

    monkeypatch.setattr(orch_role_raise, "is_signal_request", checking)
    answer("called", "looked")
    threads[0].join(timeout=30)
    assert sorted(outcomes.values()) == ["ok", f"request {row['id']} is already done"], outcomes
    answers = [event for event in _jsonl(store.root / "orch-events.jsonl") if event.get("kind") == "role-signal-answer"]
    assert [event["answer"] for event in answers] == ["called"]


def test_a_not_now_outlives_the_journal_retention_with_unchanged_facts(store, tmp_path, measured):
    """#1155: the answer lives on the request, not only in the 30-day journal."""
    measured["kinds"] = {"hot_overlap_files"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    orch_role_raise.answer(store, first["id"], "not-now", "considered")
    store.clock[0] = NOW + timedelta(days=31)
    store.trim_journals(store.clock[0])
    assert orch_role_raise.answers(store) == {}, "Retention dropped the answer event"
    assert orch_role_raise.raise_due(store, tmp_path, store.clock[0]) == []
    measured["numbers"] = {"hot_overlap_files": {"runs_by_file": {"src/a.py": 2}}}
    assert len(orch_role_raise.raise_due(store, tmp_path, store.clock[0])) == 1, "New facts still re-arm it"


def test_a_tick_between_the_done_mark_and_the_answer_event_sees_the_answer(store, tmp_path, measured, monkeypatch):
    """#1155: the not-now is set in the same locked step as done, before any event is appended."""
    measured["kinds"] = {"hot_overlap_files"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    store.clock[0] = NOW + timedelta(hours=13)
    seen = []

    def tick_in_between(*args, **kwargs):
        seen.append(orch_role_raise.raise_due(store, tmp_path, store.clock[0]))
        raise RuntimeError("answer event not written yet")

    monkeypatch.setattr(orch_role_raise.orch_events, "append_event", tick_in_between)
    with pytest.raises(RuntimeError):
        orch_role_raise.answer(store, first["id"], "not-now", "considered")
    assert seen == [[]], "Done with a not-now and unchanged facts: no new request"


def test_a_clear_outlives_the_journal_retention(store, tmp_path, measured):
    """#1155: a not-now signal that went unmet re-arms even after its clear event is trimmed."""
    measured["kinds"] = {"hot_overlap_files"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    orch_role_raise.answer(store, first["id"], "not-now", "considered")
    measured["kinds"] = set()
    store.clock[0] = NOW + timedelta(hours=1)
    orch_role_raise.raise_due(store, tmp_path, store.clock[0])
    store.clock[0] = NOW + timedelta(days=31)
    store.trim_journals(store.clock[0])
    assert clears(store) == [], "Retention dropped the clear event"
    measured["kinds"] = {"hot_overlap_files"}
    assert len(orch_role_raise.raise_due(store, tmp_path, store.clock[0])) == 1
    assert len(clears(store)) == 0 and len(orch_role_raise.raise_due(store, tmp_path, store.clock[0])) == 0


@pytest.mark.parametrize("kind", [kind for kind in orch_facts_kinds.KINDS
                                  if "binding" not in orch_facts_kinds.SHARED_FACTS.get(kind, ())])
def test_a_binding_change_leaves_a_not_now_signal_of_another_kind_quiet(store, tmp_path, measured, kind):
    """#1155: only kinds about the binding constraint fingerprint it; the others keep their own facts."""
    measured["signals"][:] = [{"paths": [], "kinds": [kind]}]
    measured["kinds"], measured["numbers"] = {kind}, {kind: {"n": 1}}
    measured["binding"] = {"kind": "cap", "normal": False, "detail": "no free slot"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    orch_role_raise.answer(store, first["id"], "not-now", "considered")
    measured["binding"] = {"kind": "quota", "normal": True, "detail": "usage-limit pause"}
    store.clock[0] = NOW + timedelta(hours=13)
    assert orch_role_raise.raise_due(store, tmp_path, store.clock[0]) == [], f"{kind} ignores the binding"
    measured["numbers"] = {kind: {"n": 2}}
    assert len(orch_role_raise.raise_due(store, tmp_path, store.clock[0])) == 1, f"{kind}'s own facts re-arm it"


def test_a_binding_change_rearms_the_kinds_that_declare_it(store, tmp_path, measured):
    kinds = [kind for kind, names in orch_facts_kinds.SHARED_FACTS.items() if "binding" in names]
    assert kinds == ["idle_lanes_with_backlog"]
    measured["signals"][:] = [{"paths": [], "kinds": kinds}]
    measured["kinds"], measured["numbers"] = set(kinds), {kinds[0]: {"n": 1}}
    measured["binding"] = {"kind": "cap", "normal": False, "detail": "slot for #41"}
    [first] = orch_role_raise.raise_due(store, tmp_path, NOW)
    orch_role_raise.answer(store, first["id"], "not-now", "considered")
    measured["binding"]["detail"] = "slot for #42"
    assert orch_role_raise.raise_due(store, tmp_path, NOW) == [], "The detail text is not a fact change"
    measured["binding"] = {"kind": "quota", "normal": True, "detail": "pause"}
    assert len(orch_role_raise.raise_due(store, tmp_path, NOW)) == 1
