"""Acceptance test for local-view. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json
import subprocess
from urllib.parse import parse_qs, urlsplit

from test_local_truth import local_cli, local_rows, record_gh, snapshot_case
from test_orch_review_handoff import make_case
from test_skills_autosync import cli_exit, isolated_home


def github_reply(case, argv):
    """Offline REST responses at the real gh boundary, backed by FakeTracker."""
    assert argv[0] == "api", f"Unexpected live command: {argv}"
    assert "-X" not in argv or argv[argv.index("-X") + 1] == "GET", argv
    endpoint = next(arg for arg in argv if arg.startswith("repos/owner/repo/"))
    url = urlsplit(endpoint)
    parts = url.path.removeprefix("repos/owner/repo/").split("/")
    query = parse_qs(url.query)
    tracker = case.tracker

    def issue_data(issue):
        return {"number": issue.number, "title": issue.title, "body": issue.body,
                "state": issue.state, "labels": [{"name": label} for label in sorted(issue.labels)],
                "updated_at": case.clock[0].isoformat()}

    if parts == ["issues"]:
        return json.dumps([issue_data(issue) for issue in tracker.list_issues(query.get("state", ["all"])[0])])
    if parts == ["pulls"]:
        prs = tracker.created_prs
        if "head" in query:
            head = query["head"][0].split(":", 1)[-1]
            prs = [pr for pr in prs if pr.head == head]
        state = query.get("state", ["open"])[0]
        return json.dumps([{"number": pr.number, "title": pr.title, "body": pr.body,
                            "head": {"ref": pr.head}, "base": {"ref": pr.base},
                            "merged_at": pr.merged_at, "html_url": f"https://github.com/owner/repo/pull/{pr.number}"}
                           for pr in prs if state == "all" or tracker.get_issue(pr.number).state == state])
    assert parts[0] == "issues", f"Unexpected live endpoint: {endpoint}"
    number = int(parts[1])
    suffix = parts[2:]
    if not suffix:
        return json.dumps(issue_data(tracker.get_issue(number)))
    if suffix == ["parent"]:
        parent = tracker.parent_of(number)
        if parent is None:
            raise subprocess.CalledProcessError(1, ["gh", *argv], stderr="HTTP 404: Not Found")
        return json.dumps(issue_data(tracker.get_issue(parent)))
    if suffix == ["sub_issues"]:
        return json.dumps([issue_data(issue) for issue in tracker.sub_issues(number)])
    if suffix == ["dependencies", "blocked_by"]:
        return json.dumps([{"number": dep.number, "state": dep.state, "state_reason": dep.state_reason}
                           for dep in tracker.blocked_by(number)])
    if suffix == ["comments"]:
        return json.dumps([{"id": comment.id, "body": comment.body, "user": {"login": comment.author},
                            "created_at": comment.created_at} for comment in tracker.list_comments(number)])
    if suffix == ["events"]:
        return json.dumps([{"label": {"name": event.label}, "event": event.action,
                            "actor": {"login": event.actor}, "created_at": event.created_at}
                           for event in tracker.label_events(number)])
    raise AssertionError(f"Unexpected live endpoint: {endpoint}")


def test_local_view_hundred_refreshes_use_local_order_children_titles_and_completions(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import watch
    from foragent.tracker import GhTracker

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    main = local_cli(case, monkeypatch)
    case.tracker.update_issue(99, title="Remote title must wait for the gate", body="")
    case.tracker.set_parent(1, 2)
    calls = record_gh(monkeypatch)
    tracker = GhTracker("owner/repo")
    for tick in range(100):
        assert main(["gate", "status", "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        snapshot = watch.collect(status, tracker, case.store, case.config, case.clock[0])
        frame = watch.render_frame(snapshot, case.config, case.root, tick=tick, width=160)
        assert snapshot.order.number == 99 and snapshot.order.title == "Ship the local dashboard"
        assert (snapshot.completed, snapshot.total) == (1, 4)
        assert [issue.number for issue in snapshot.done["api"]] == [4]
        assert all(title in frame for title in ("Ship the local dashboard", "Implement feature", "Finished API task"))
        assert "Remote title" not in frame
    assert calls == []


def test_local_view_gate_status_is_local_and_reports_snapshot_time(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    loaded_at = case.clock[0]
    case.clock[0] += timedelta(minutes=2)
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert datetime.fromisoformat(payload["as_of"]) == loaded_at
    assert payload["summary"] == {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 0}
    assert payload["lanes"]["api"]["title"] == "Implement feature"
    assert payload["lanes"]["review"]["issue"] == 2
    assert payload["lanes"]["decision"]["issue"] == 3
    assert calls == []


def test_local_view_live_status_reads_github_refreshes_local_records_and_as_of(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    case.clock[0] += timedelta(minutes=2)
    case.tracker.record_label(2, "needs-human", "human")
    case.tracker.update_issue(2, title="Human updated the review", body="")
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch, reply=lambda argv: github_reply(case, argv))
    assert cli_exit(main, ["gate", "status", "--live", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls, "Explicit --live status must perform a GitHub read"
    assert abs((datetime.fromisoformat(payload["as_of"]) - case.clock[0]).total_seconds()) <= 1
    assert payload["lanes"]["review"]["title"] == "Human updated the review"
    assert payload["lanes"]["review"]["state"] == "waiting-human"
    row = local_rows(case.store)[2]
    assert row["title"] == "Human updated the review" and "needs-human" in row["labels"]
    # The next ordinary status must reuse the refresh, with no additional read.
    cached_calls = record_gh(monkeypatch)
    assert main(["gate", "status", "--json"]) == 0
    cached = json.loads(capsys.readouterr().out)
    assert cached["as_of"] == payload["as_of"] and cached["lanes"] == payload["lanes"]
    assert cached_calls == []


def test_local_view_missing_snapshot_status_is_successful_unknown_and_offline(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    main = local_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "as_of" in payload and payload["as_of"] is None
    assert all(not issues for issues in payload["issues"].values())
    assert calls == [] and local_rows(case.store) == {}


def test_local_view_failed_gate_refresh_preserves_data_as_of_and_failure_time(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_orch_review_handoff import tick_context
    from foragent.scheduler import tick
    from foragent.state import StateStore
    from foragent.tracker import GhTracker, TrackerError

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    loaded_at = case.clock[0]
    case.clock[0] += timedelta(minutes=5, seconds=1)
    failed_at = case.clock[0]
    case.tracker.update_issue(2, title="Unrefreshed remote title", body="")
    case.tracker.record_label(2, "needs-human", "human")
    main = local_cli(case, monkeypatch)
    error = "HTTP 503: GitHub unavailable"

    def unavailable(argv):
        raise subprocess.CalledProcessError(1, ["gh", *argv], stderr=error)

    refresh_calls = record_gh(monkeypatch, reply=unavailable)
    ctx = tick_context(case, start_runs=False)
    ctx.tracker = GhTracker("owner/repo")
    try:
        tick(ctx)
    except TrackerError:
        pass  # Gate exit policy is separate from preserving observation facts.
    assert refresh_calls, "The due gate refresh must actually attempt a read"

    # A new reader must see durable failure facts. Even if GitHub recovers,
    # an ordinary status cannot replace them with an unrequested live read.
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    calls = record_gh(monkeypatch, reply=lambda argv: github_reply(case, argv))
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("stale") is True, "A failed refresh must mark the last known data stale"
    assert error in payload["error"]
    assert payload["summary"] == {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 0}
    assert datetime.fromisoformat(payload["as_of"]) == loaded_at
    assert payload["lanes"]["review"]["title"] == "Review dispute"
    assert payload["lanes"]["review"]["state"] == "blocked"
    rows = local_rows(case.store)
    assert set(rows) == {1, 2, 3, 4, 99}
    assert rows[2]["title"] == "Review dispute" and "needs-human" not in rows[2]["labels"]
    assert rows[4]["merged"] is True and rows[4]["pr_number"] is not None
    # The persistence layout is private; require the failure and its time to
    # be recorded together without prescribing a filename or field name.
    records = [path.read_text(encoding="utf-8") for path in case.store.root.rglob("*.json")]
    assert any(error in record and failed_at.isoformat() in record for record in records), (
        "The refresh failure time must be persisted separately from the last successful as_of"
    )
    assert calls == []


def test_local_view_failed_initial_refresh_json_summary_is_null(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_orch_review_handoff import tick_context
    from foragent.scheduler import tick
    from foragent.tracker import GhTracker, TrackerError

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, outcome="cancelled")
    main = local_cli(case, monkeypatch)

    def timeout(argv):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    refresh_calls = record_gh(monkeypatch, reply=timeout)
    ctx = tick_context(case, start_runs=False)
    ctx.tracker = GhTracker("owner/repo")
    try:
        tick(ctx)
    except TrackerError:
        pass
    assert refresh_calls
    # An empty remote response must not become a substitute for a missing
    # local snapshot, even if an observer incorrectly tries another read.
    calls = record_gh(monkeypatch, reply=lambda argv: "[]")
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {
        "working": None, "blocked": None, "waiting_human": None, "queued": None,
    }, "Without usable data, zero would claim a known empty queue"
    assert payload["as_of"] is None
    assert payload["stale"] is True and payload["error"]
    assert payload["active_runs"] == []
    assert calls == [] and local_rows(case.store) == {}


def test_local_view_failed_initial_refresh_korean_text_warns_instead_of_zero(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_orch_review_handoff import tick_context
    from foragent.scheduler import tick
    from foragent.tracker import GhTracker, TrackerError

    case = make_case(git_history, tmp_path, monkeypatch, language="ko",
                     skill_libraries=[], test_command="true")
    case.store.finish_run(1, outcome="cancelled")
    main = local_cli(case, monkeypatch)

    def timeout(argv):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    refresh_calls = record_gh(monkeypatch, reply=timeout)
    ctx = tick_context(case, start_runs=False)
    ctx.tracker = GhTracker("owner/repo")
    try:
        tick(ctx)
    except TrackerError:
        pass
    assert refresh_calls
    calls = record_gh(monkeypatch, reply=lambda argv: "[]")
    assert main(["gate", "status"]) == 0
    output = capsys.readouterr().out
    assert "⚠ 상태를 못 읽었어요" in output
    assert "일하는 중 0" not in output
    assert calls == []


def test_local_view_live_timeout_json_returns_success_with_stale_local_numbers(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = snapshot_case(git_history, tmp_path, monkeypatch)
    loaded_at = case.clock[0]
    case.clock[0] += timedelta(minutes=2)
    main = local_cli(case, monkeypatch)

    def timeout(argv):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    calls = record_gh(monkeypatch, reply=timeout)
    assert cli_exit(main, ["gate", "status", "--live", "--json"]) == 0, (
        "A failed live observation must return a usable stale status, not a CLI error"
    )
    payload = json.loads(capsys.readouterr().out)
    assert calls, "--live must attempt the GitHub read that timed out"
    assert payload["stale"] is True and payload["error"]
    assert payload["summary"] == {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 0}
    assert datetime.fromisoformat(payload["as_of"]) == loaded_at
    assert payload["lanes"]["api"]["issue"] == 1
    assert payload["lanes"]["review"]["title"] == "Review dispute"


def test_local_view_live_timeout_korean_text_warns_and_keeps_last_known_numbers(
    git_history, tmp_path, monkeypatch, capsys,
):
    from dataclasses import replace

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    case.config = case.ctx.config = replace(case.config, language="ko")
    loaded_at = case.clock[0]
    case.clock[0] += timedelta(minutes=2)
    main = local_cli(case, monkeypatch)

    def timeout(argv):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    calls = record_gh(monkeypatch, reply=timeout)
    assert cli_exit(main, ["gate", "status", "--live"]) == 0, (
        "A timeout must print the warning and cached numbers while exiting successfully"
    )
    output = capsys.readouterr().out
    assert calls, "--live must attempt the GitHub read that timed out"
    assert "⚠ 상태를 못 읽었어요" in output
    assert "일하는 중 1" in output and "일하는 중 0" not in output
    # The failed live read must also remain visible to the next local reader.
    live_calls = list(calls)
    assert main(["gate", "status", "--json"]) == 0
    cached = json.loads(capsys.readouterr().out)
    assert cached["stale"] is True and cached["error"]
    assert cached["summary"] == {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 0}
    assert datetime.fromisoformat(cached["as_of"]) == loaded_at
    assert calls == live_calls


def test_local_view_stale_snapshot_frame_warns_on_first_line_with_active_run(
    git_history, tmp_path, monkeypatch, capsys,
):
    from dataclasses import replace
    from foragent import watch

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    case.config = case.ctx.config = replace(case.config, language="ko")
    main = local_cli(case, monkeypatch)
    record_gh(monkeypatch, reply=lambda argv: github_reply(case, argv))
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [run.number for run in case.store.active_runs()] == [1]
    # Exercise rendering independently of refresh/error persistence: this is
    # the public status shape required after a failed refresh of known data.
    payload.update(stale=True, error="GitHub read timed out after 120 seconds",
                   as_of=case.clock[0].isoformat())
    snapshot = watch.collect(payload, case.tracker, case.store, case.config, case.clock[0])
    calls = record_gh(monkeypatch)
    frame = watch.render_frame(snapshot, case.config, case.root, width=160)
    assert "⚠ 상태를 못 읽었어요" in frame.splitlines()[0]
    assert "일하는 중 1" in frame and "일하는 중 0" not in frame
    assert "Implement feature" in frame
    assert calls == []


def test_local_view_failed_refresh_counts_five_local_runs_without_issue_snapshot(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_orch_review_handoff import tick_context
    from foragent.scheduler import tick
    from foragent.tracker import GhTracker, TrackerError

    lanes = {f"worker-{number}": {"paths": [f"worker-{number}/**"]} for number in range(1, 6)}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, max_parallel=5,
                     skill_libraries=[], test_command="true")
    for number in range(1, 6):
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number), lane=f"worker-{number}")
        case.store.update_run(number, phase="implementing")
    main = local_cli(case, monkeypatch)

    def timeout(argv):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    refresh_calls = record_gh(monkeypatch, reply=timeout)
    ctx = tick_context(case, start_runs=False)
    ctx.tracker = GhTracker("owner/repo")
    try:
        tick(ctx)
    except TrackerError:
        pass
    assert refresh_calls
    assert [run.number for run in case.store.active_runs()] == [1, 2, 3, 4, 5]
    calls = record_gh(monkeypatch, reply=lambda argv: "[]")
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {run["number"] for run in payload["active_runs"]} == {1, 2, 3, 4, 5}
    assert payload["summary"]["working"] == 5, (
        "Local run records establish working=5 even when no tracker snapshot is available"
    )
    assert all(payload["summary"][key] is None for key in ("blocked", "waiting_human", "queued"))
    assert payload["stale"] is True and payload["error"]
    for number in range(1, 6):
        lane = payload["lanes"][f"worker-{number}"]
        assert lane["state"] == "working" and lane["issue"] == number
    assert calls == []


def test_local_view_failed_refresh_counts_runs_started_after_last_snapshot(
    git_history, tmp_path, monkeypatch, capsys,
):
    from dataclasses import replace
    from test_orch_review_handoff import tick_context
    from foragent.scheduler import tick
    from foragent.tracker import GhTracker, TrackerError

    case = snapshot_case(git_history, tmp_path, monkeypatch)
    loaded_at = case.clock[0]
    case.clock[0] += timedelta(minutes=5, seconds=1)
    new_lanes = {f"worker-{number}": {"paths": [f"worker-{number}/**"]} for number in range(10, 14)}
    case.config = case.ctx.config = replace(case.config, lanes=case.config.lanes | new_lanes,
                                           max_parallel=5)
    # These runs started after the last successful gate read and have no
    # tracker issue records. Retaining the old summary alone would miss them.
    for number in range(10, 14):
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number), lane=f"worker-{number}")
    main = local_cli(case, monkeypatch)

    def timeout(argv):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    refresh_calls = record_gh(monkeypatch, reply=timeout)
    ctx = tick_context(case, start_runs=False)
    ctx.tracker = GhTracker("owner/repo")
    try:
        tick(ctx)
    except TrackerError:
        pass
    assert refresh_calls
    assert len(case.store.active_runs()) == 5
    calls = record_gh(monkeypatch, reply=lambda argv: github_reply(case, argv))
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"working": 5, "blocked": 1, "waiting_human": 1, "queued": 0}, (
        "Stale tracker data must be combined with current local runs, counting each run once"
    )
    assert payload["stale"] is True and payload["error"]
    assert datetime.fromisoformat(payload["as_of"]) == loaded_at
    assert calls == []
