"""CI job durations (#2497): every source is filtered before it is cut, and a refresh merges."""

from datetime import UTC, datetime, timedelta

from foragent import orch_facts_ci
from foragent.state import StateStore
from foragent.tracker import WORKFLOW_RUNS_PAGE, FakeTracker

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
WORKFLOW = """jobs:
  test:
    timeout-minutes: 45
    strategy: {matrix: {shard: [0]}}
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


def test_train_runs_behind_a_full_page_of_traffic_are_found(tmp_path, monkeypatch):
    """A pull_request train run is asked for by its branch name, so PR traffic cannot hide it."""
    monkeypatch.setattr(orch_facts_ci, "_train_branches", lambda root: ["foragent/batch-merge-9"])
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


def test_the_train_search_reports_its_page_bound_when_a_job_stays_unfound(tmp_path, monkeypatch):
    """#2560: a listing pages until each target is found, the listing ends, or its page bound,
    and the record reports which one, plus the targets no run has completed."""
    monkeypatch.setattr(orch_facts_ci, "JOB_READS", 10 ** 6)
    store, tracker = case(tmp_path)
    cancelled = [run(5000 + i, "main", NOW - timedelta(minutes=i), 3, conclusion="cancelled")
                 for i in range((orch_facts_ci.SEARCH_PAGES + 1) * WORKFLOW_RUNS_PAGE)]
    tracker.set_workflow_runs(cancelled)
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW)
    found = orch_facts_ci.search(store)
    by_source = {row.get("branch") or row.get("event"): row for row in found["listings"]}
    assert (by_source["main"]["pages"], by_source["main"]["stopped"]) == (orch_facts_ci.SEARCH_PAGES, "page_limit")
    assert found["page_limit"] == orch_facts_ci.SEARCH_PAGES
    assert found["never_found_jobs"] == [".github/workflows/ci.yml: test (0)"]

    tracker.set_workflow_runs([run(1, "main", NOW + timedelta(minutes=1), 41)] + cancelled)
    job = observe(store, tracker, tmp_path, NOW + orch_facts_ci.REFRESH)
    assert job["durations_minutes"] == [41.0]
    again = orch_facts_ci.search(store)
    assert {row.get("branch") or row.get("event"): row["stopped"] for row in again["listings"]}["main"] == "found"
    assert again["never_found_jobs"] == []


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


def test_each_matrix_shard_is_its_own_target(tmp_path):
    """#2560 review round 3: shard 1 cancelled early in a recent train run must not count as found;
    its latest 40-of-45-minute train run behind 99 PR runs is still measured and alarms."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/ci.yml").write_text(
        WORKFLOW.replace("shard: [0]", "shard: [0, 1]"), encoding="utf-8")

    def sharded(run_id, branch, started, minutes, event="push", conclusions=("success", "success")):
        value = run(run_id, branch, started, minutes[0], event=event)
        value["jobs"] = [{"name": f"test ({k})", "conclusion": conclusions[k], "started_at": stamp(started),
                          "completed_at": stamp(started + timedelta(minutes=minutes[k]))} for k in range(2)]
        return value

    # A full page of newer train runs where shard 0 succeeds and shard 1 is cancelled early.
    recent = [sharded(100 + i, f"foragent/batch-merge-{200 + i}", NOW - timedelta(minutes=1 + i), (10, 2),
                      conclusions=("success", "cancelled")) for i in range(WORKFLOW_RUNS_PAGE)]
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=2 + i), 5, event="pull_request")
               for i in range(99)]
    latest = sharded(2, "foragent/batch-merge-1", NOW - timedelta(hours=3), (10, 40))
    older = [sharded(10 + k, "main", NOW - timedelta(hours=5 + k), (10, 20)) for k in range(6)]
    tracker.set_workflow_runs(recent + traffic + [latest] + older)
    for k in range(4):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    measured = {job["job"]: job for job in orch_facts_ci.jobs(store)}
    assert measured["test (1)"]["latest_minutes"] == 40.0 and measured["test (1)"]["near_limit"]
    assert "test (1)" in orch_facts_ci.kind_numbers(list(measured.values()))["ci_job_near_limit"]["jobs"]


def test_a_train_branch_gone_from_the_remote_is_listed_once_more(tmp_path, monkeypatch):
    """A pull_request train run that finishes just before its branch lands is still read, once."""
    remote = [["foragent/batch-merge-5"]]
    monkeypatch.setattr(orch_facts_ci, "_train_branches", lambda root: remote[0])
    store, tracker = case(tmp_path)
    train = run(9, "foragent/batch-merge-5", NOW - timedelta(minutes=50), 39, event="pull_request")
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=i), 5, event="pull_request")
               for i in range(WORKFLOW_RUNS_PAGE + 5)]
    tracker.set_workflow_runs(traffic)  # The train run is still in progress.
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW)
    remote[0] = []
    tracker.set_workflow_runs(traffic + [train])
    asked = []
    listing = tracker.recent_workflow_runs
    tracker.recent_workflow_runs = lambda **kw: asked.append(kw.get("branch")) or listing(**kw)
    assert observe(store, tracker, tmp_path, NOW + orch_facts_ci.REFRESH)["durations_minutes"] == [39.0]
    assert "foragent/batch-merge-5" in asked
    asked.clear()
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + 2 * orch_facts_ci.REFRESH)
    assert "foragent/batch-merge-5" not in asked


def test_measured_runs_are_asked_for_directly_so_pr_traffic_never_fills_the_pages(tmp_path):
    """#2560 (PM q77): GitHub filters by workflow and by branch or event before paging, so more PR runs
    than the page bound ahead of a pushed train run never hide it."""
    store, tracker = case(tmp_path)
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=i), 5, event="pull_request")
               for i in range((orch_facts_ci.SEARCH_PAGES + 1) * WORKFLOW_RUNS_PAGE)]
    train = run(7, "foragent/batch-merge-9", NOW - timedelta(days=3), 38)
    tracker.set_workflow_runs(traffic + [train])
    asked = []
    listing = tracker.recent_workflow_runs
    tracker.recent_workflow_runs = lambda **kw: asked.append(kw) or listing(**kw)
    assert observe(store, tracker, tmp_path, NOW)["durations_minutes"] == [38.0]
    assert asked and all(kw.get("workflow") == ".github/workflows/ci.yml" and (kw.get("branch") or kw.get("event"))
                         for kw in asked), asked


def test_a_pull_request_train_whose_branch_is_gone_is_found_at_startup(tmp_path):
    """#2560 review round 4: with an empty record, a finished pull_request train whose branch was
    already deleted sits behind 500 newer PR runs; its 40-of-45-minute duration still alarms."""
    store, tracker = case(tmp_path)
    traffic = [run(1000 + i, f"foragent/issue-{i}", NOW - timedelta(minutes=i), 5, event="pull_request")
               for i in range(500)]
    train = run(7, "foragent/batch-merge-gone", NOW - timedelta(hours=9), 40, event="pull_request")
    older = [run(10 + k, "main", NOW - timedelta(hours=10 + k), 20) for k in range(6)]
    tracker.set_workflow_runs(traffic + [train] + older)
    for k in range(3):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    [job] = orch_facts_ci.jobs(store)
    assert job["latest_minutes"] == 40.0 and job["near_limit"]


def test_a_shard_only_include_adds_is_its_own_target(tmp_path):
    """#2560 review round 4: `matrix: {shard: [0], include: [{shard: 1}]}` runs shard 1 too; 100
    newer train runs completing only shard 0 must not hide shard 1's latest 40-minute run."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/ci.yml").write_text(
        WORKFLOW.replace("shard: [0]}", "shard: [0], include: [{shard: 1}]}"), encoding="utf-8")
    recent = [run(100 + i, f"foragent/batch-merge-{200 + i}", NOW - timedelta(minutes=1 + i), 10)
              for i in range(WORKFLOW_RUNS_PAGE)]
    latest = run(2, "foragent/batch-merge-1", NOW - timedelta(hours=3), 40)
    latest["jobs"][0]["name"] = "test (1)"
    older = []
    for k in range(6):
        old = run(10 + k, "main", NOW - timedelta(hours=5 + k), 20)
        old["jobs"][0]["name"] = "test (1)"
        older.append(old)
    tracker.set_workflow_runs(recent + [latest] + older)
    for k in range(5):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    measured = {job["job"]: job for job in orch_facts_ci.jobs(store)}
    assert measured["test (1)"]["latest_minutes"] == 40.0 and measured["test (1)"]["near_limit"]


def test_targets_follow_githubs_matrix_rules():
    """Each job and shard GitHub runs is a target named as GitHub names it; a name GitHub decides at
    run time is a prefix."""
    names = lambda text: [(t["name"], t["prefix"]) for t in orch_facts_ci.workflow_targets(text)]
    assert names("jobs: {a: {strategy: {matrix: {shard: [0], include: [{shard: 1}]}}}}") == [
        ("a (0)", None), ("a (1)", None)]
    assert names("jobs: {a: {strategy: {matrix: {include: [{os: x}, {os: y, n: 2}]}}}}") == [
        ("a (x)", None), ("a (y, 2)", None)]
    assert names("jobs: {a: {strategy: {matrix: {os: [l, m], n: [1, 2], exclude: [{os: m, n: 2}],"
                 " include: [{os: l, extra: e}]}}}}") == [("a (l, 1, e)", None), ("a (l, 2, e)", None),
                                                           ("a (m, 1)", None)]
    assert names('jobs: {a: {strategy: {matrix: "${{ fromJSON(x) }}"}}, b: {uses: ./x.yml},'
                 ' c: {name: "t ${{ matrix.s }}"}}') == [("a", " ("), ("b", " / "), ("t", "")]


import pytest  # noqa: E402


@pytest.mark.parametrize("job, names", [
    ('test:\n    timeout-minutes: 45\n    strategy: {matrix: "${{ fromJSON(vars.SHARDS) }}"}', ("test (0)", "test (1)")),
    ('test:\n    name: "test ${{ matrix.s }}"\n    timeout-minutes: 45\n    strategy: {matrix: {s: "${{ fromJSON(x) }}"}}',
     ("test 0", "test 1")),
    ('test:\n    uses: ./.github/workflows/inner.yml', ("test / a", "test / b")),
], ids=["runtime-matrix", "expression-name", "reusable-workflow"])
def test_a_runtime_named_target_needs_each_shard_it_runs(tmp_path, job, names):
    """#2560 review round 5: a target GitHub names at run time splits into the shards runs show, so
    100 newer train runs completing one shard and cancelling the other cannot hide the other's
    latest 40-minute run on page 2."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/ci.yml").write_text(f"jobs:\n  {job}\n", encoding="utf-8")
    first, second = names

    def jobs(started, minutes, conclusions):
        return [{"name": name, "conclusion": conclusion, "started_at": stamp(started),
                 "completed_at": stamp(started + timedelta(minutes=m))}
                for name, m, conclusion in zip(names, minutes, conclusions)]

    runs = []
    for i in range(WORKFLOW_RUNS_PAGE):
        value = run(100 + i, f"foragent/batch-merge-{200 + i}", NOW - timedelta(minutes=1 + i), 10)
        value["jobs"] = jobs(NOW - timedelta(minutes=1 + i), (10, 2), ("success", "cancelled"))
        runs.append(value)
    latest = run(2, "foragent/batch-merge-1", NOW - timedelta(hours=3), 10)
    latest["jobs"] = jobs(NOW - timedelta(hours=3), (10, 40), ("success", "success"))
    older = []
    for k in range(6):
        value = run(10 + k, "main", NOW - timedelta(hours=5 + k), 10)
        value["jobs"] = jobs(NOW - timedelta(hours=5 + k), (10, 20), ("success", "success"))
        older.append(value)
    tracker.set_workflow_runs(runs + [latest] + older)
    for k in range(8):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    measured = {job["job"]: job for job in orch_facts_ci.jobs(store)}
    if job.startswith("test:\n    uses"):
        return  # A reusable workflow's jobs have no limit here; their durations are what is measured.
    assert measured[second]["latest_minutes"] == 40.0 and measured[second]["near_limit"]


def test_a_project_without_workflows_starts_no_process(tmp_path, monkeypatch):
    """A refresh with no workflow file in the checkout asks no git command and no GitHub listing."""
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError(f"started a process: {args}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    store, tracker = StateStore(tmp_path / "state"), FakeTracker()
    orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW)
    assert orch_facts_ci.jobs(store) == []


def test_a_runtime_shard_seen_only_without_times_is_still_sought(tmp_path):
    """A runtime shard that newer runs show only as a skipped job with no times keeps its name, so
    the search still pages to its latest 40-minute run instead of stopping on its sibling."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/ci.yml").write_text(
        'jobs:\n  test:\n    timeout-minutes: 45\n    strategy: {matrix: "${{ fromJSON(vars.SHARDS) }}"}\n',
        encoding="utf-8")
    runs = []
    for i in range(WORKFLOW_RUNS_PAGE):
        value = run(100 + i, f"foragent/batch-merge-{200 + i}", NOW - timedelta(minutes=1 + i), 10)
        value["jobs"].append({"name": "test (1)", "conclusion": "skipped", "started_at": None, "completed_at": None})
        runs.append(value)
    latest = run(2, "foragent/batch-merge-1", NOW - timedelta(hours=3), 10)
    latest["jobs"].append({"name": "test (1)", "conclusion": "success", "started_at": stamp(NOW - timedelta(hours=3)),
                           "completed_at": stamp(NOW - timedelta(hours=3) + timedelta(minutes=40))})
    tracker.set_workflow_runs(runs + [latest])
    for k in range(8):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    measured = {job["job"]: job for job in orch_facts_ci.jobs(store)}
    assert measured["test (1)"]["latest_minutes"] == 40.0 and measured["test (1)"]["near_limit"]


@pytest.mark.parametrize("job, names", [
    ('test:\n    timeout-minutes: 45\n    strategy: {matrix: "${{ fromJSON(vars.SHARDS) }}"}', ("test (0)", "test (1)")),
    ('test:\n    name: "test ${{ matrix.s }}"\n    timeout-minutes: 45\n    strategy: {matrix: {s: "${{ fromJSON(x) }}"}}',
     ("test 0", "test 1")),
    ('test:\n    uses: ./.github/workflows/inner.yml', ("test / a", "test / b")),
], ids=["runtime-matrix", "expression-name", "reusable-workflow"])
@pytest.mark.parametrize("skipped_hint", [False, True], ids=["unseen", "seen-skipped-once"])
def test_a_runtime_shard_absent_from_the_first_page_is_still_found(tmp_path, job, names, skipped_hint):
    """#2560 review round 6: at startup with no live train branch, 100 newer train runs show only the
    first runtime shard (once, optionally, the second as skipped without times); the second shard's
    latest 40-minute run on page 2 is still measured after eight refreshes."""
    store, tracker = case(tmp_path)
    (tmp_path / ".github/workflows/ci.yml").write_text(f"jobs:\n  {job}\n", encoding="utf-8")
    first, second = names

    def job_row(name, started, minutes, conclusion="success"):
        return {"name": name, "conclusion": conclusion, "started_at": stamp(started),
                "completed_at": stamp(started + timedelta(minutes=minutes))}

    runs = []
    for i in range(WORKFLOW_RUNS_PAGE):
        started = NOW - timedelta(minutes=1 + i)
        value = run(100 + i, f"foragent/batch-merge-{200 + i}", started, 10)
        value["jobs"] = [job_row(first, started, 10)]
        if skipped_hint and i == 20:
            value["jobs"].append({"name": second, "conclusion": "skipped", "started_at": None, "completed_at": None})
        runs.append(value)
    started = NOW - timedelta(hours=3)
    latest = run(2, "foragent/batch-merge-1", started, 10)
    latest["jobs"] = [job_row(first, started, 10), job_row(second, started, 40)]
    tracker.set_workflow_runs(runs + [latest])
    for k in range(8):
        orch_facts_ci.observe(store, tracker, tmp_path, "main", NOW + k * orch_facts_ci.REFRESH)
    measured = {row["job"]: row for row in orch_facts_ci.jobs(store)}
    assert measured[second]["latest_minutes"] == 40.0
    assert orch_facts_ci.search(store)["never_found_jobs"] == [], "found runtime targets are not reported missing"
    if "uses" not in job:  # A called workflow's limits live in the called file.
        assert measured[second]["near_limit"]
