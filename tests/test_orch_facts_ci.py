"""CI job durations (#2497): every source is filtered before it is cut, and a refresh merges."""

from datetime import UTC, datetime, timedelta

from foragent import orch_facts_ci
from foragent.state import StateStore
from foragent.tracker import WORKFLOW_RUNS_PAGE, FakeTracker

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
WORKFLOW = """jobs:
  test:
    timeout-minutes: 45
    steps: [{run: "true"}]
"""


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(run_id, branch, started, minutes, event="push", conclusion="success"):
    return {"id": run_id, "path": ".github/workflows/ci.yml", "head_branch": branch, "event": event,
            "status": "completed", "run_started_at": stamp(started),
            "jobs": [{"name": "test (0)", "conclusion": conclusion, "started_at": stamp(started),
                      "completed_at": stamp(started + timedelta(minutes=minutes))}]}


def case(tmp_path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text(WORKFLOW, encoding="utf-8")
    return StateStore(tmp_path / "state"), FakeTracker()


def observe(store, tracker, root, now):
    orch_facts_ci.observe(store, tracker, root, "main", now)
    [job] = orch_facts_ci.jobs(store)
    return job


def test_pull_request_traffic_never_hides_main_runs(tmp_path):
    store, tracker = case(tmp_path)
    main = [run(i, "main", NOW - timedelta(hours=10 - i), 40) for i in range(3)]
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=i), 5, event="pull_request")
               for i in range(orch_facts_ci.TRAIN_PAGES * WORKFLOW_RUNS_PAGE + 10)]
    tracker.set_workflow_runs(traffic + list(reversed(main)))
    job = observe(store, tracker, tmp_path, NOW)
    assert job["durations_minutes"] == [40.0, 40.0, 40.0]
    assert orch_facts_ci.kind_numbers([job])["ci_job_near_limit"]["jobs"]["test (0)"]["latest_minutes"] == 40.0


def test_train_runs_behind_a_full_page_of_traffic_are_found(tmp_path):
    store, tracker = case(tmp_path)
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=i), 5, event="pull_request")
               for i in range(WORKFLOW_RUNS_PAGE + 5)]
    train = run(7, "foragent/batch-merge-9", NOW - timedelta(hours=5), 38, event="pull_request")
    tracker.set_workflow_runs(traffic + [train])
    assert observe(store, tracker, tmp_path, NOW)["durations_minutes"] == [38.0]


def test_a_refresh_merges_with_the_record_instead_of_replacing_it(tmp_path):
    store, tracker = case(tmp_path)
    tracker.set_workflow_runs([run(2, "main", NOW - timedelta(hours=1), 30), run(1, "main", NOW - timedelta(hours=2), 20)])
    observe(store, tracker, tmp_path, NOW)
    # The next listing no longer shows the older runs; what was measured stays.
    tracker.set_workflow_runs([run(3, "main", NOW, 36)])
    job = observe(store, tracker, tmp_path, NOW + orch_facts_ci.REFRESH)
    assert job["durations_minutes"] == [20.0, 30.0, 36.0]


def test_runs_stay_until_each_job_has_its_window_of_measured_durations(tmp_path):
    store, tracker = case(tmp_path)
    measured = [run(i, "main", NOW - timedelta(hours=40 - i), 10 + i) for i in range(orch_facts_ci.WINDOW)]
    skipped = [run(100 + i, "main", NOW - timedelta(hours=20 - i), 0, conclusion="skipped") for i in range(10)]
    tracker.set_workflow_runs(list(reversed(measured + skipped)))
    job = observe(store, tracker, tmp_path, NOW)
    assert job["durations_minutes"] == [float(10 + i) for i in range(orch_facts_ci.WINDOW)]
    # A later refresh keeps them too: newer skipped runs never push measured runs out.
    tracker.set_workflow_runs([run(200, "main", NOW, 0, conclusion="skipped")])
    job = observe(store, tracker, tmp_path, NOW + orch_facts_ci.REFRESH)
    assert job["durations_minutes"] == [float(10 + i) for i in range(orch_facts_ci.WINDOW)]


def test_a_run_read_once_is_not_read_again_while_listed(tmp_path, monkeypatch):
    store, tracker = case(tmp_path)
    tracker.set_workflow_runs([run(1, "main", NOW, 0, conclusion="skipped")])
    reads = []
    jobs = tracker.workflow_run_jobs
    monkeypatch.setattr(tracker, "workflow_run_jobs", lambda run_id: reads.append(run_id) or jobs(run_id))
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW)
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + orch_facts_ci.REFRESH)
    assert reads == [1]
