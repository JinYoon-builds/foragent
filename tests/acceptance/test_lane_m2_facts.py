"""Acceptance test for lane-m2-facts. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
import pytest

from test_lane_view_d76 import lane_d76_case
from test_skills_autosync import isolated_home
from test_squirrel_running import single_worker, view_offline


def test_collect_exposes_role_liveness_and_caller_copy_without_summarising_why(tmp_path, monkeypatch):
    from foragent import orch_architect, watch_data

    case = lane_d76_case(tmp_path, monkeypatch)
    now = case.store._now()
    case.store.root.mkdir(parents=True, exist_ok=True)
    record = {"pid": 88001, "started_at": (now - timedelta(hours=2)).isoformat(),
              "why": "Full detailed evidence: keep every clause (do not split).", "short": "레인 경계를 살펴봐요"}
    (case.store.root / "architect.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(orch_architect, "session_alive", lambda rec: bool(rec and rec.get("pid") == 88001))
    snapshot = watch_data.collect(case.status, case.tracker, case.store, case.config, now)
    facts = getattr(snapshot, "role_facts", None)
    assert isinstance(facts, dict) and "architect" in facts, "the view lacks collected role facts, not a prose classifier"
    assert facts["architect"]["alive"] is True
    assert facts["architect"]["short"] == record["short"]
    assert facts["architect"]["last_called_at"] == record["started_at"]


def test_collect_exposes_week_counts_recorded_first_pass_and_newest_memory(tmp_path):
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker
    from foragent.watch_data import collect

    snapshot, config = single_worker()
    config = replace(config, lanes={"work": {"paths": ["work/**"], "agent": "fa-work"}})
    now = snapshot.captured_at.astimezone().replace(hour=18)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    clock = [monday]
    store, tracker = StateStore(tmp_path / "state", now=lambda: clock[0]), FakeTracker()
    for number, lane, when, verdicts in ((90, "work", monday - timedelta(hours=1), ["PASS"]),
                                        (91, "work", monday + timedelta(hours=1), ["PASS"]),
                                        (92, "work", monday + timedelta(hours=2), ["FAIL", "PASS"]),
                                        (93, "work", monday + timedelta(hours=3), ["PASS"]),
                                        (94, "elsewhere", monday + timedelta(hours=4), ["PASS"])):
        clock[0] = when - timedelta(minutes=10)
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane=lane)
        tracker.add_issue(number, f"Finished {number}", labels=[f"lane:{lane}"], state="closed")
        with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
            for i, verdict in enumerate(verdicts, 1):
                stream.write(json.dumps({"at": when.isoformat(), "kind": "review", "issue": number,
                                         "round": i, "verdict": verdict}) + "\n")
        clock[0] = when
        store.finish_run(number, "merged")
    clock[0] = now
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, now)
    memory = tmp_path / ".claude/agent-memory/fa-work/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# Memory\nOld lesson\n\n最新 lesson: keep the configured locale\n", encoding="utf-8")
    facts = getattr(collect(snapshot.status, tracker, store, config, now, repo_root=tmp_path), "lane_facts", None)
    assert isinstance(facts, dict) and "work" in facts, "collect must give the renderer local lane facts"
    assert facts["work"]["owned_paths"] == ["work/**"]
    assert facts["work"]["week_finished"] == 3
    assert (facts["work"]["week_first_pass"], facts["work"]["week_reviewed"]) == (2, 3)
    assert facts["work"]["memory_latest"] == "最新 lesson: keep the configured locale"


@pytest.mark.parametrize('phase,expected', [('overlap-wait','file-overlap'),('test-slot-wait','capacity'),
    ('limit-wait','quota'),('baseline-wait',None),('executor-wait',None)])
def test_wait_phase_kinds_are_recorded_facts_and_unknown_phases_ignore_full_meter(tmp_path,monkeypatch,phase,expected):
    from foragent import watch_data, orch_facts, overlap_files
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker
    snapshot, config = single_worker(phase=phase)
    now = snapshot.captured_at
    store, tracker = StateStore(tmp_path/'state',now=lambda:now), FakeTracker()
    tracker.add_issue(101,'Task',labels=['approved','running','lane:work'])
    store.refresh_local_issues(tracker.list_issues('all'),[],config.lanes,now)
    store.start_run(101,branch='b',worktree='w',lane='work')
    store.update_run(101,phase=phase)
    monkeypatch.setattr(watch_data,'meter',lambda *a: {'running':9,'budget':3,'memory':'critical'})
    monkeypatch.setattr(orch_facts,'collect',lambda *a,**k: pytest.fail('watch must not replay admission'))
    monkeypatch.setattr(overlap_files,'claims',lambda *a,**k: pytest.fail('no root means no concrete-overlap helper'))
    actual = watch_data.collect(snapshot.status,tracker,store,config,now)
    assert actual.waiting_facts[101] == {'kind':expected,'blocking_issues':[],'overlapping_files':[]}


def test_meter_capacity_applies_to_all_queued_lanes_and_excludes_held_people(tmp_path,monkeypatch):
    from foragent import watch_data, status
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker
    snapshot, config = single_worker()
    config = replace(config,lanes={'work':{},'other':{}},max_parallel=17)
    now = snapshot.captured_at
    store, tracker = StateStore(tmp_path/'state',now=lambda:now), FakeTracker()
    for number,labels in [(201,['approved','ready','lane:work']),(202,['approved','ready','lane:other']),
                          (203,['approved','needs-human','lane:work']),(204,['approved','blocked','lane:other'])]:
        tracker.add_issue(number,'Task',labels=labels)
    store.refresh_local_issues(tracker.list_issues('all'),[],config.lanes,now)
    observed = {'running':3,'budget':3}
    monkeypatch.setattr(watch_data,'meter',lambda *a: dict(observed))
    payload = status.collect(store,config,now)
    actual = watch_data.collect(payload,tracker,store,config,now)
    assert actual.waiting_facts[201]['kind'] == actual.waiting_facts[202]['kind'] == 'capacity'
    assert not {203,204} & actual.waiting_facts.keys()
    observed['running']=2
    actual = watch_data.collect(payload,tracker,store,config,now)
    assert actual.waiting_facts[201]['kind'] is None and actual.waiting_facts[202]['kind'] is None
