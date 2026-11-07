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
               for i in range(orch_facts_ci.SEARCH_PAGES * WORKFLOW_RUNS_PAGE + 10)]
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


def test_the_train_search_reports_its_page_bound_when_a_job_stays_unfound(tmp_path):
    """#2560: the search pages past runs that skip a job until it finds the job's latest run, says so
    when it stops at its page bound instead, and does not repeat that deep search on each refresh."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/release.yml").write_text(
        "jobs:\n  publish:\n    steps: [{run: \"true\"}]\n", encoding="utf-8")  # Never runs on main or train.
    skipping = [run(2000 + i, f"foragent/batch-merge-{i}", NOW - timedelta(minutes=i), 0, conclusion="skipped")
                for i in range(3)]
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=10 + i), 5, event="pull_request")
               for i in range((orch_facts_ci.SEARCH_PAGES + 1) * WORKFLOW_RUNS_PAGE)]
    train = run(7, "foragent/batch-merge-9", NOW - timedelta(days=3), 38)
    tracker.set_workflow_runs(skipping + traffic + [train])
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW)
    assert orch_facts_ci.jobs(store) == []
    found = orch_facts_ci.search(store)
    assert (found["pages"], found["page_limit"], found["stopped"], found["unsearched_jobs"]) == (
        orch_facts_ci.SEARCH_PAGES, orch_facts_ci.SEARCH_PAGES, "page_limit",
        [".github/workflows/ci.yml: test", ".github/workflows/release.yml: publish"])

    pages = []
    listing = tracker.recent_workflow_runs
    tracker.recent_workflow_runs = lambda **kw: pages.append(kw.get("page")) or listing(**kw)
    newer = run(8, "foragent/batch-merge-10", NOW + timedelta(minutes=1), 41)
    tracker.set_workflow_runs([newer] + skipping + traffic + [train])
    job = observe(store, tracker, tmp_path, NOW + orch_facts_ci.REFRESH)
    assert job["durations_minutes"] == [41.0]
    again = orch_facts_ci.search(store)
    assert (again["stopped"], again["unsearched_jobs"]) == ("page_limit", [".github/workflows/release.yml: publish"])
    assert [page for page in pages if page is not None and page > 1] == [], f"Deep search repeated: {pages}"


def test_a_workflow_no_recorded_run_names_is_still_searched(tmp_path):
    """#2560 review: a recent main run of workflow A must not end the search before workflow B's
    latest train run, which sits behind 99 PR runs."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/b.yml").write_text(
        "jobs:\n  slow:\n    timeout-minutes: 45\n    steps: [{run: \"true\"}]\n", encoding="utf-8")
    main = run(1, "main", NOW - timedelta(minutes=1), 10)
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=2 + i), 5, event="pull_request")
               for i in range(99)]
    late = run(2, "foragent/batch-merge-1", NOW - timedelta(hours=3), 40)
    late["path"] = ".github/workflows/b.yml"
    late["jobs"][0]["name"] = "slow"
    tracker.set_workflow_runs([main] + traffic + [late])
    for k in range(3):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    measured = {job["job"]: job for job in orch_facts_ci.jobs(store)}
    assert measured["slow"]["durations_minutes"] == [40.0] and measured["slow"]["near_limit"]
    assert orch_facts_ci.kind_numbers(list(measured.values()))["ci_job_near_limit"]["jobs"]["slow"]["latest_minutes"] == 40.0


def test_skipped_jobs_without_times_do_not_hide_the_jobs_latest_run(tmp_path):
    """#2560 review: GitHub may give a skipped job no times; 40 such train runs plus 60 PR runs ahead
    of the job's latest 40-minute train run must not leave it at its older main durations."""
    store, tracker = case(tmp_path)
    runs = []
    for i in range(100):
        if i % 5 < 2:
            skipped = run(2000 + i, f"foragent/batch-merge-{i}", NOW - timedelta(minutes=i), 0, conclusion="skipped")
            skipped["jobs"][0].update(started_at=None, completed_at=None)
            runs.append(skipped)
        else:
            runs.append(run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=i), 9, event="pull_request"))
    runs.append(run(7, "foragent/batch-merge-999", NOW - timedelta(hours=3), 40))
    runs += [run(10 + k, "main", NOW - timedelta(hours=5 + k), 20) for k in range(6)]
    tracker.set_workflow_runs(runs)
    for k in range(3):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    [job] = orch_facts_ci.jobs(store)
    assert job["latest_minutes"] == 40.0 and job["near_limit"]
