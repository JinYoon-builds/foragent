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
