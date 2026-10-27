"""The tick's binding events and the scorecard's binding line (D96)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from foragent import orch_binding, orch_inbox, orch_scorecard
from foragent import orch_scorecard_flow as flow
from foragent.config import Config
from foragent.issue import body_hash
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


def _waiting(store, number, at=NOW, files="src/a.py", exclusive=None):
    header = f"exclusive: [{exclusive}]\n" if exclusive else ""
    body = (f"<!-- foragent:v1 -->\n{header}## Goal\nG\n## Files\n- {files}\n## Out of scope\nO\n"
            "## Done when\n```sh\ntrue\n```\n")
    store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=at.isoformat())
    store.update_local_issue(number, title="t", body=body, labels=["approved", "ready"], state="open", blocked_by=[])


def _tick(store, config, tmp_path, sample=("normal", 0.1)):
    return orch_binding.from_tick(store, config, tmp_path, NOW, sample)


def test_from_tick_reads_held_work_without_probes(tmp_path):
    store, config = _store(tmp_path), Config()
    assert _tick(store, config, tmp_path) == "none"
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    store.update_run(5, phase="test-slot-wait", phase_started_at=NOW.isoformat())
    assert _tick(store, config, tmp_path) == "test_slots"
    # As in fa orch facts, a GitHub wait holds waiting issues; a run waiting in flight is not one.
    store.pause_github(NOW + timedelta(hours=1))
    assert _tick(store, config, tmp_path) == "test_slots"
    _waiting(store, 7)
    assert _tick(store, config, tmp_path) == "github"
    assert _tick(store, config, tmp_path, ("critical", 0.1)) == "machine"


def test_from_tick_names_quota_for_a_usage_paused_lane_like_facts(tmp_path):
    store, config = _store(tmp_path), Config()
    _waiting(store, 7)
    assert _tick(store, config, tmp_path) == "none", "Facts' start loop would start it"
    store.pause_executor(config.executor, NOW + timedelta(hours=1))
    assert _tick(store, config, tmp_path) == "quota"


def _budget(tmp_path, monkeypatch, at, parallel):
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    capacity.append_budget_history(tmp_path, at, "parallel", 0, parallel, pressure="normal",
                                   load_per_cpu=0.1, swapins=0)


def test_rule_follows_the_start_loop_order(tmp_path, monkeypatch):
    """Replayed day ends: an occupied slot holds work (cap), a free one meets the lane and file rules."""
    store, config = _store(tmp_path), Config()
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 1)
    end = NOW - timedelta(days=1)
    _waiting(store, 7, at=end - timedelta(hours=1), files="src/a.py")
    store.clock[0] = end - timedelta(minutes=30)
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    store.clock[0] = NOW
    assert flow.binding_at(store, config, tmp_path, end, current=False) == "cap", (
        "A run still going at the replayed moment holds its slot")
    store.finish_run(5, outcome="error")
    assert flow.binding_at(store, config, tmp_path, end, current=False) == "cap", (
        "A run that finished after the moment held its slot then")
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none"


def test_replay_applies_the_file_rule_against_runs_holding_slots(tmp_path, monkeypatch):
    store, config = _store(tmp_path), Config(overlap_per_file=1)
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 3)
    _waiting(store, 5, at=NOW - timedelta(hours=2), files="src/a.py")
    store.clock[0] = NOW - timedelta(hours=1)
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    store.update_local_issue(5, labels=["approved", "running"])
    _waiting(store, 7, at=NOW - timedelta(hours=1), files="src/a.py")
    store.clock[0] = NOW
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "structure"
    _waiting(store, 7, at=NOW - timedelta(hours=1), files="src/b.py")
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none"


def test_scorecard_replays_each_days_binding_without_events(tmp_path):
    store = _store(tmp_path)
    store.clock[0] = NOW - timedelta(days=1, minutes=10)
    store.start_run(10, branch="foragent/issue-10", worktree=store.worktree_path(10))
    store.update_run(10, phase="overlap-wait", phase_started_at=store.clock[0].isoformat())
    store.clock[0] = NOW - timedelta(days=1)
    store.finish_run(10, outcome="killed")
    store.clock[0] = NOW
    card = orch_scorecard.collect(store, 2)
    assert orch_scorecard.binding(store, Config(), tmp_path, card) == {"today": "none", "previous": "structure"}


def test_record_tick_uses_the_tick_context(tmp_path):
    store = _store(tmp_path)
    ctx = SimpleNamespace(store=store, config=Config(), repo_root=tmp_path, now=lambda: NOW,
                          _budget_sample=("normal", 0.1))
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


def test_replay_claims_committed_measured_files_like_facts(tmp_path, monkeypatch):
    """A run that committed measured files claims those, not its declared Files, now and in history."""
    store, config = _store(tmp_path), Config(overlap_per_file=1)
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 3)
    _waiting(store, 5, at=NOW - timedelta(hours=3), files="src/a.py")
    store.clock[0] = NOW - timedelta(hours=2)
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    store.update_run(5, files_committed=True, effective_files=["src/b.py"])
    store.update_local_issue(5, labels=["approved", "running"])
    _waiting(store, 7, at=NOW - timedelta(hours=2), files="src/b.py")
    store.clock[0] = NOW
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "structure"
    store.finish_run(5, outcome="error")
    assert flow.binding_at(store, config, tmp_path, NOW - timedelta(hours=1), current=False) == "structure", (
        "The finished run's history record keeps its measured files")
    _waiting(store, 7, at=NOW - timedelta(hours=2), files="src/a.py")
    assert flow.binding_at(store, config, tmp_path, NOW - timedelta(hours=1), current=False) == "none", (
        "Its declared file is no longer claimed")


def _labelled(store, number, at):
    store.update_local_issue(number, title="t", labels=["approved", "ready"], state="open", blocked_by=[])
    store.record_read(number, "label_events", [
        {"label": label, "action": "labeled", "actor": "human", "created_at": at.isoformat()}
        for label in ("approved", "ready")], baseline=store.read_baseline())


def test_replay_candidates_need_an_approval_snapshot_like_facts(tmp_path, monkeypatch):
    store, config = _store(tmp_path), Config()
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 1)
    for number in (7, 8):
        _labelled(store, number, NOW - timedelta(hours=1))
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none", (
        "Facts' waiting list skips issues with no approved snapshot")
    for number, files in ((7, "src/a.py"), (8, "src/b.py")):
        _waiting(store, number, at=NOW - timedelta(hours=1), files=files)
        _labelled(store, number, NOW - timedelta(hours=1))
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "cap"


def test_replay_candidates_skip_the_blocker_rule_like_facts(tmp_path, monkeypatch):
    store, config = _store(tmp_path), Config()
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 1)
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    _waiting(store, 7, at=NOW - timedelta(hours=1))
    store.update_local_issue(7, blocked_by=[{"number": 99, "state": "open", "repository": None}])
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "cap", (
        "Facts counts a ready issue with an open blocker as waiting")


def test_from_tick_does_not_probe_the_file_system(tmp_path, monkeypatch):
    """Ranking and the file rule run, but the tick's binding asks the file system nothing."""
    from foragent import scheduler

    calls = []
    real = scheduler._filesystem_case_sensitive
    monkeypatch.setattr(scheduler, "_filesystem_case_sensitive", lambda root: calls.append(root) or real(root))
    store, config = _store(tmp_path), Config(overlap_per_file=1)
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 3)
    _waiting(store, 5, at=NOW - timedelta(hours=2), files="src/a.py")
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    store.update_local_issue(5, labels=["approved", "running"])
    _waiting(store, 7, files="src/a.py")
    _waiting(store, 8, files="src/b.py")
    # Facts' start order: #8 overlaps nothing and takes the last free slot, so #7 waits on cap.
    assert _tick(store, config, tmp_path) == "cap"
    assert calls == [], "No file-system probe during the tick's binding"
    from foragent import orch_facts_resources
    from foragent.orch_facts_runs import _waiting_issues

    running = store.active_runs()
    orch_facts_resources._admission(store, config, tmp_path, NOW, _waiting_issues(store, config, running), running,
                                    machine={"budget": {"max_parallel": None}, "alive": {}})
    assert calls, "fa orch facts itself still checks the case rule"


def _running(store, number, files, exclusive=None):
    _waiting(store, number, at=NOW - timedelta(hours=2), files=files, exclusive=exclusive)
    store.clock[0] = NOW - timedelta(hours=1)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.update_local_issue(number, labels=["approved", "running"])
    store.clock[0] = NOW


def test_unknown_case_rule_never_makes_different_paths_overlap(tmp_path, monkeypatch):
    """The tick knows no case rule; running src/a.py and waiting src/b.py at overlap_per_file=1 hold nothing."""
    store, config = _store(tmp_path), Config(overlap_per_file=1)
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 3)
    _running(store, 5, "src/a.py")
    _waiting(store, 7, files="src/b.py")
    assert _tick(store, config, tmp_path) == "none", "Spare capacity and different files: no structure binding"
    assert orch_binding.at(store, NOW) is None
    ctx = SimpleNamespace(store=store, config=config, repo_root=tmp_path, now=lambda: NOW,
                          _budget_sample=("normal", 0.1))
    orch_binding.record_tick(ctx)
    assert orch_binding.at(store, NOW) == "none", "The tick records no structure binding"
    _waiting(store, 7, files="src/a.py")
    assert _tick(store, config, tmp_path) == "structure", "The same path still overlaps"


def test_unknown_case_rule_keeps_exclusive_reservations_and_ranking_to_their_paths(tmp_path, monkeypatch):
    from foragent import overlap_files
    from foragent.scheduler_start import _overlap_count

    store, config = _store(tmp_path), Config(overlap_per_file=1)
    _budget(tmp_path, monkeypatch, NOW - timedelta(days=3), 3)
    _running(store, 5, "src/a.py", exclusive="src/z.py")
    _waiting(store, 7, files="src/b.py")
    assert _tick(store, config, tmp_path) == "none", "An exclusive reservation of src/z.py leaves src/b.py free"
    _waiting(store, 7, files="src/z.py")
    assert _tick(store, config, tmp_path) == "structure", "It still reserves its own path"
    ctx = SimpleNamespace(store=store, config=config, repo_root=tmp_path, case_sensitive=None)
    known, _ = overlap_files.claims(ctx, measured=False)
    _waiting(store, 7, files="src/b.py")
    assert _overlap_count(ctx, 7, known) == 0, "Ranking counts no overlap between different paths"
    assert overlap_files.intersects(ctx, "src/a.py", "src/A.py") is False, "Unknown is case-sensitive"
    assert overlap_files.intersects(ctx, "src/a.py", "src/a.py") is True


def test_replay_reads_the_parallel_state_file_like_facts(tmp_path, monkeypatch):
    """With no budget history, parallel-state.json holds its value from its changed_at."""
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store, config = _store(tmp_path), Config()
    _waiting(store, 7, at=NOW - timedelta(hours=3), files="src/a.py")
    _waiting(store, 8, at=NOW - timedelta(hours=3), files="src/b.py")
    capacity.write_state(store, 1, NOW - timedelta(hours=1))
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "cap", "One slot for two issues"
    assert _tick(store, config, tmp_path) == "cap", "fa orch facts' own rule agrees"
    assert flow.binding_at(store, config, tmp_path, NOW - timedelta(hours=2), current=False) == "none", (
        "Before its changed_at the file says nothing; facts' default budget then fits both")


def test_replay_uses_the_recorded_machine_sample_like_facts(tmp_path, monkeypatch):
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store, config = _store(tmp_path), Config()
    _waiting(store, 7, at=NOW - timedelta(hours=3))
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none"
    capacity.append_memory_hold(tmp_path, NOW - timedelta(minutes=10), pressure="critical",
                                load_per_cpu=0.2, swapins=0)
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "machine", "Critical memory was recorded"
    assert _tick(store, config, tmp_path, ("critical", 0.2)) == "machine", "Facts says the same for that sample"
    capacity.append_budget_history(tmp_path, NOW - timedelta(minutes=5), "parallel", 2, 3, pressure="normal",
                                   load_per_cpu=capacity.OVERLOAD_LOAD, swapins=0)
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "machine", "Overload load was recorded"
    capacity.append_budget_history(tmp_path, NOW - timedelta(minutes=1), "parallel", 3, 4, pressure="normal",
                                   load_per_cpu=0.1, swapins=0)
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none", "The latest sample is normal"
    assert flow.binding_at(store, config, tmp_path, NOW - timedelta(minutes=7), current=False) == "machine"


def test_replay_candidates_use_facts_labels_not_idles_assumed_ready(tmp_path, monkeypatch):
    from foragent import capacity

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store, config = _store(tmp_path), Config()
    capacity.write_state(store, 1, NOW - timedelta(days=1))
    for number, files in ((7, "src/a.py"), (8, "src/b.py")):
        _waiting(store, number, at=NOW - timedelta(hours=3), files=files)
        store.update_local_issue(number, labels=["approved"])  # Approved, not ready; no label events.
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none", "Facts needs ready"
    assert _tick(store, config, tmp_path) == "none"
    for number in (7, 8):
        store.update_local_issue(number, labels=["approved", "ready", "running"])
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "none", "A running label holds it"
    for number in (7, 8):
        store.update_local_issue(number, labels=["approved", "ready"])
    assert flow.binding_at(store, config, tmp_path, NOW, current=True) == "cap"
    assert _tick(store, config, tmp_path) == "cap"
