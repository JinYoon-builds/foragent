"""Recorded lane-view facts: parks, today's merges, waiting causes, roles and lane memory."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from foragent import status, watch_data, watch_lane_facts, watch_role_facts
from foragent.config import Config
from foragent.issue import body_hash
from foragent.state import StateStore
from foragent.tracker import FakeTracker

NOW = datetime(2026, 10, 21, 15, tzinfo=UTC)


def journal(store, *events):
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write((event if isinstance(event, str) else json.dumps(event)) + "\n")


def setup(tmp_path, issues, config):
    store, tracker = StateStore(tmp_path / "state", now=lambda: NOW), FakeTracker()
    for number, labels in issues:
        tracker.add_issue(number, f"Task {number}", labels=labels)
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    return store, tracker


def approve(store, number, files, exclusive=()):
    body = "<!-- foragent:v1 -->\n"
    if exclusive:
        body += "exclusive:\n" + "".join(f"  - {path}\n" for path in exclusive)
    body += "## Files\n" + "".join(f"- {path}\n" for path in files)
    store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(hours=1)).isoformat())


def test_park_facts_follow_the_last_valid_park_or_unpark_event(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    journal(store, {"kind": "parked", "issue": 5, "at": "2026-10-21T10:00:00+00:00"},
            {"kind": "unparked", "issue": 5, "at": "2026-10-21T11:00:00+00:00"},
            {"kind": "parked", "issue": 6, "at": "2026-10-21T12:00:00+00:00"},
            {"kind": "unparked", "issue": 6, "at": "not a time"}, '{"kind": "parked", "issue": 7')
    facts = watch_lane_facts.park_facts(watch_lane_facts.journal(store))
    assert facts == {5: {"parked": False, "at": "2026-10-21T11:00:00+00:00"},
                     6: {"parked": True, "at": "2026-10-21T12:00:00+00:00"}}


def test_merged_today_keeps_older_keys_and_takes_the_newer_valid_time():
    now = NOW.astimezone().replace(hour=12, minute=0, second=0, microsecond=0)  # Local noon.
    old = now - timedelta(days=3)
    known = {1: now - timedelta(hours=5), 2: old, 7: now - timedelta(minutes=30)}
    work = [(1, "m", None, now - timedelta(hours=1)), (3, "m", None, now - timedelta(hours=2)),
            (4, "m", None, now + timedelta(hours=1)), (5, "m", None, now.replace(tzinfo=None)),
            (6, "m", None, now - timedelta(days=1)), (2, "m", None, now - timedelta(days=2)),
            (7, "m", None, now - timedelta(hours=1))]
    assert watch_lane_facts.merged_today(known, work, now) == {
        1: now - timedelta(hours=1), 2: old, 3: now - timedelta(hours=2), 7: now - timedelta(minutes=30)}


def test_dependency_comes_first_and_unapproved_issues_with_open_edges_wait(tmp_path, monkeypatch):
    config = Config(lanes={"work": {}})
    store, tracker = setup(tmp_path, [(301, ["approved", "ready", "lane:work"]), (302, ["lane:work"]),
                                      (303, ["approved", "ready", "lane:work"]),
                                      (304, ["approved", "running", "needs-human", "lane:work"])], config)
    store.start_run(304, branch="b", worktree="w", lane="work")
    store.update_run(304, phase="test-slot-wait")
    rows = store.local_issues()
    edges = {301: [{"number": 303, "state": "open"}], 302: [{"number": 999, "state": "open"}]}
    store.local_issues = lambda: [dict(row, blocked_by=edges.get(row["number"], [])) for row in rows]
    monkeypatch.setattr(watch_data, "meter", lambda *a: {"running": 5, "budget": 5})
    facts = watch_data.collect(status.collect(store, config, NOW), tracker, store, config, NOW).waiting_facts
    assert facts[301] == {"kind": "dependency", "blocking_issues": [303], "overlapping_files": []}
    assert facts[302] == {"kind": "dependency", "blocking_issues": [999], "overlapping_files": []}
    assert facts[303]["kind"] == "capacity"
    assert 304 not in facts, "a person's question is a held fact, not a wait"


def test_load_or_memory_holds_read_as_machine_and_a_normal_full_budget_as_capacity(tmp_path, monkeypatch):
    config = Config(lanes={"work": {}})
    store, tracker = setup(tmp_path, [(401, ["approved", "ready", "lane:work"])], config)
    observed = {"running": 1, "budget": 1, "memory": "normal"}
    monkeypatch.setattr(watch_data, "meter", lambda *a: dict(observed))

    def kind():
        payload = status.collect(store, config, NOW)
        return watch_data.collect(payload, tracker, store, config, NOW).waiting_facts[401]["kind"]

    assert kind() == "capacity"
    history = store.root.parent / "budget-history.jsonl"
    rows = [{"budget": "parallel", "from": 4, "to": 2, "load_per_cpu": 2.0},
            {"budget": "test", "from": 3, "to": 2, "load_per_cpu": 0.1},
            {"budget": "parallel", "from": 2, "to": 1, "load_per_cpu": 1.7}]
    history.write_text("".join(json.dumps(row) + "\n" for row in rows) + "{broken\n", encoding="utf-8")
    assert kind() == "machine"
    history.write_text(history.read_text() + json.dumps(
        {"budget": "parallel", "from": 2, "to": 1, "load_per_cpu": 0.4, "wanted": 1}) + "\n")
    assert kind() == "capacity", "a lower ceiling is not the computer being busy"
    observed.update(running=0, memory="critical")
    assert kind() is None, "a memory sample alone is no recorded hold"
    hold = {"at": (NOW - timedelta(minutes=5)).isoformat(), "rule": "memory-critical", "pressure": "critical"}
    history.write_text(history.read_text() + json.dumps(hold) + "\n")
    assert kind() == "machine", "the newest start attempt was held by memory"
    held = history.read_text()
    history.write_text(held + json.dumps(
        {"at": NOW.isoformat(), "budget": "parallel", "from": 1, "to": 2, "pressure": "normal"}) + "\n")
    assert kind() is None, "a later budget row sampled normal memory"
    history.write_text(held)
    assert kind() == "machine"
    store.start_run(402, branch="b", worktree="w", lane="work")  # Started after the hold (at NOW).
    assert kind() is None, "a later start ends the recorded hold"


def test_concrete_overlap_needs_a_root_and_lists_only_shared_concrete_files(tmp_path, monkeypatch):
    config = Config(lanes={"work": {}}, overlap_per_file=1)
    store, tracker = setup(tmp_path, [(501, ["approved", "running", "lane:work"]),
                                      (502, ["approved", "ready", "lane:work"])], config)
    approve(store, 501, ["src/a.py", "docs/**"])
    store.start_run(501, branch="b", worktree=str(tmp_path / "w"), lane="work")
    approve(store, 502, ["src/a.py", "src/b.py", "docs/guide.md"])
    monkeypatch.setattr(watch_data, "meter", lambda *a: {"running": 1, "budget": 5})
    payload = status.collect(store, config, NOW)
    rooted = watch_data.collect(payload, tracker, store, config, NOW, repo_root=tmp_path).waiting_facts
    assert rooted[502] == {"kind": "file-overlap", "blocking_issues": [],
                           "overlapping_files": ["docs/guide.md", "src/a.py"]}
    assert 501 not in rooted, "ordinary active work without waiting evidence is not waiting"
    unrooted = watch_data.collect(payload, tracker, store, config, NOW).waiting_facts
    assert unrooted[502] == {"kind": None, "blocking_issues": [], "overlapping_files": []}


def test_unknown_wait_phase_takes_a_concrete_overlap_from_active_claims(tmp_path, monkeypatch):
    config = Config(lanes={"work": {}}, overlap_per_file=1)
    store, tracker = setup(tmp_path, [(601, ["approved", "running", "lane:work"]),
                                      (602, ["approved", "running", "lane:work"])], config)
    approve(store, 601, ["src/a.py"])
    store.start_run(601, branch="b", worktree=str(tmp_path / "w1"), lane="work")
    approve(store, 602, ["src/a.py", "src/c.py"])
    store.start_run(602, branch="b2", worktree=str(tmp_path / "w2"), lane="work")
    store.update_run(602, phase="baseline-wait")
    monkeypatch.setattr(watch_data, "meter", lambda *a: {"running": 9, "budget": 1})
    payload = status.collect(store, config, NOW)
    facts = watch_data.collect(payload, tracker, store, config, NOW, repo_root=tmp_path).waiting_facts
    assert facts[602] == {"kind": "file-overlap", "blocking_issues": [], "overlapping_files": ["src/a.py"]}
    assert 601 not in facts, "ordinary active work is not waiting, even with a full meter"


def test_exclusive_reservations_hold_and_shared_paths_show_below_the_limit(tmp_path, monkeypatch):
    config = Config(lanes={"work": {}}, overlap_per_file=2)
    store, tracker = setup(tmp_path, [(701, ["approved", "running", "lane:work"]),
                                      (702, ["approved", "ready", "lane:work"]),
                                      (703, ["approved", "ready", "lane:work"]),
                                      (704, ["approved", "ready", "lane:work"])], config)
    approve(store, 701, ["src/a.py"], exclusive=["src/x.py"])
    store.start_run(701, branch="b", worktree=str(tmp_path / "w"), lane="work")
    approve(store, 702, ["src/x.py"])
    approve(store, 703, ["src/a.py", "src/**/*.md"])
    approve(store, 704, ["docs/n.md"], exclusive=["src/a.py"])
    monkeypatch.setattr(watch_data, "meter", lambda *a: {"running": 1, "budget": 5})
    payload = status.collect(store, config, NOW)
    facts = watch_data.collect(payload, tracker, store, config, NOW, repo_root=tmp_path).waiting_facts
    assert facts[702] == {"kind": "file-overlap", "blocking_issues": [], "overlapping_files": ["src/x.py"]}
    assert facts[703] == {"kind": None, "blocking_issues": [], "overlapping_files": ["src/a.py"]}
    assert facts[704] == {"kind": "file-overlap", "blocking_issues": [], "overlapping_files": ["src/a.py"]}


def test_review_times_without_a_zone_are_skipped_not_fatal(tmp_path):
    config = Config(lanes={"work": {}})
    clock = [NOW - timedelta(hours=2)]
    store, tracker = StateStore(tmp_path / "state", now=lambda: clock[0]), FakeTracker()
    tracker.add_issue(801, "Done", labels=["lane:work"], state="closed")
    store.start_run(801, branch="b", worktree="w", lane="work")
    journal(store, {"kind": "review", "issue": 801, "round": 1, "verdict": "FAIL",
                    "at": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()},
            {"kind": "review", "issue": 801, "round": 2, "verdict": "PASS",
             "at": (NOW - timedelta(hours=1)).isoformat()},
            {"kind": "review", "issue": 802, "round": 1, "verdict": "PASS",
             "at": (NOW - timedelta(hours=1)).isoformat()})
    clock[0] = NOW - timedelta(minutes=30)
    store.finish_run(801, "merged")
    clock[0] = NOW
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    work = [(801, "m", NOW - timedelta(hours=2), NOW - timedelta(minutes=30)),
            (802, "m", (NOW - timedelta(hours=3)).replace(tzinfo=None), NOW - timedelta(minutes=20))]
    events = watch_lane_facts.journal(store)
    facts = watch_lane_facts.lane_facts({"lanes": {}}, store, config, NOW, None, work, events,
                                        {802: {"labels": ["lane:work"]}}, store.read_history())
    work_facts = facts["work"]
    # Round 1's zone-less time is unusable, so round 2 is the first review; 802's start has no zone.
    assert (work_facts["week_finished"], work_facts["week_reviewed"], work_facts["week_first_pass"]) == (2, 1, 1)


def test_unreadable_history_and_journal_read_as_unknown_instead_of_failing(tmp_path, monkeypatch):
    config = Config(lanes={"work": {}})
    store, tracker = setup(tmp_path, [(901, ["approved", "ready", "lane:work"])], config)
    payload = status.collect(store, config, NOW)
    (store.root / "orch-events.jsonl").mkdir()  # Opening it fails with an OSError.
    (store.root.parent / "budget-history.jsonl").write_text(json.dumps(
        {"at": NOW.isoformat(), "rule": "memory-critical", "pressure": "critical"}) + "\n")

    def unreadable():
        raise PermissionError("runs.jsonl")

    monkeypatch.setattr(store, "read_history", unreadable)
    monkeypatch.setattr(watch_data, "meter", lambda *a: {"running": 0, "budget": 2})
    snapshot = watch_data.collect(payload, tracker, store, config, NOW)
    assert snapshot.lane_facts["work"]["week_finished"] is None
    assert snapshot.lane_facts["work"]["week_reviewed"] is None
    assert snapshot.today_done is None and snapshot.done == {}
    assert snapshot.park_facts == {} and snapshot.phases == {} and snapshot.events == []
    assert snapshot.waiting_facts[901]["kind"] is None, "no readable history proves no current hold"


def test_a_merge_started_by_an_active_run_counts_for_that_runs_lane():
    store = SimpleNamespace(read_history=list)
    config = Config(lanes={"work": {}, "other": {}})
    start = NOW - timedelta(hours=2)
    active = {"number": 951, "lane": "work", "started_at": start.isoformat()}
    facts = watch_lane_facts.lane_facts(
        {"lanes": {}, "active_runs": [active]}, store, config, NOW, None,
        [(951, "m", start, NOW - timedelta(hours=1))], [], {951: {"labels": ["lane:other"]}}, [])
    assert (facts["work"]["week_finished"], facts["other"]["week_finished"]) == (1, 0)
    unknown = watch_lane_facts.lane_facts(
        {"lanes": {}, "active_runs": [active]}, store, config, NOW, None,
        [(951, "m", start, NOW - timedelta(hours=1))], [], {}, None)
    assert unknown["work"]["week_finished"] is None, "without the run history no merge has a known run"


def test_role_facts_default_to_never_called_and_ignore_the_full_why(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.root.mkdir(parents=True)
    (store.root / "docs.json").write_text(json.dumps({"pid": -1, "why": "long", "started_at": "x"}))
    assert watch_role_facts.collect(store) == {
        "architect": {"alive": False, "short": None, "last_called_at": None},
        "docs": {"alive": False, "short": None, "last_called_at": "x"}}


def test_lane_memory_uses_the_explicit_agent_and_no_root_or_status_lane_reads_none(tmp_path):
    config = Config(lanes={"api": {"agent": "custom"}, "web": {"agent": " "}})
    store, tracker = setup(tmp_path, [], config)
    for agent, text in (("custom", "- api lesson\n"), ("fa-api", "wrong\n"), ("fa-web", "# h\n- web lesson\n# t\n")):
        path = tmp_path / ".claude/agent-memory" / agent / "MEMORY.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    payload = status.collect(store, config, NOW)
    facts = watch_data.collect(payload, tracker, store, config, NOW, repo_root=tmp_path).lane_facts
    assert facts["api"]["memory_latest"] == "- api lesson"
    assert facts["web"]["memory_latest"] == "- web lesson"
    assert facts["main"] == {"owned_paths": [], "week_finished": 0, "week_first_pass": 0,
                             "week_reviewed": 0, "memory_latest": None}
    with watch_data.collection_root(tmp_path):
        bound = watch_data.collect(payload, tracker, store, config, NOW).lane_facts
    assert bound["api"]["memory_latest"] == "- api lesson"
    assert watch_data.collect(payload, tracker, store, config, NOW).lane_facts["api"]["memory_latest"] is None
