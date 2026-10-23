from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import subprocess

import pytest

from foragent.tracker import (
    Comment, Dependency, FakeTracker, GhTracker, Issue, LabelEvent, LabelSpec, PullRequest, TrackerError,
)


REPO = "owner/repo"
NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)


@pytest.mark.parametrize("numbers,expected", [([], None), ([42], 42)])
def test_find_existing_open_pr_gh(numbers, expected):
    run = ScriptedRun(json.dumps([{"number": number} for number in numbers]))
    assert GhTracker(REPO, run).find_pr("foragent/issue-1") == expected
    assert run.calls == [([
        "api", "--paginate",
        "repos/owner/repo/pulls?state=open&head=owner%3Aforagent%2Fissue-1&per_page=100",
    ], None)]


def test_find_existing_open_pr_fake():
    tracker = FakeTracker(strict=True)
    assert tracker.find_pr("feature") is None
    pr = tracker.create_pr(head="feature", base="main", title="Title", body="Closes #1")
    assert tracker.find_pr("feature") == pr
    tracker.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
    assert tracker.find_pr("feature") is None


def test_recording_gh_writes_need_no_extra_reads_and_failed_writes_leave_snapshot(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    store = StateStore(tmp_path, now=lambda: NOW)
    store.record_issue(Issue(1, "Feature", "body", frozenset({"approved"}), "open"), {})
    run = ScriptedRun("[]", gh_error("HTTP 422 validation failed", code=422), "")
    tracker = RecordingTracker(GhTracker(REPO, run),
                               SimpleNamespace(store=store, config=Config(), now=lambda: NOW))
    tracker.add_labels(1, ["blocked"])
    with pytest.raises(TrackerError, match="422"):
        tracker.remove_labels(1, ["approved"])
    tracker.close_issue(1)
    row, = StateStore(tmp_path).local_issues()
    assert row["labels"] == ["approved", "blocked"] and row["state"] == "closed"
    assert [args[:3] for args, _ in run.calls] == [
        ["api", "-X", "POST"], ["api", "-X", "DELETE"], ["issue", "close", "1"],
    ]


@pytest.mark.parametrize("first_missing", [False, True])
@pytest.mark.parametrize("failure", [403, 429])
def test_recording_label_removal_preserves_each_success_before_failure(tmp_path, first_missing, failure):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    store = StateStore(tmp_path, now=lambda: NOW)
    store.record_issue(Issue(1, "Feature", "body", frozenset({"blocked", "ready", "running"}), "open"), {})
    run = ScriptedRun(gh_error("HTTP 404") if first_missing else "[]", gh_error(f"HTTP {failure}"))
    tracker = RecordingTracker(GhTracker(REPO, run), SimpleNamespace(store=store, config=Config()))
    with pytest.raises(TrackerError, match=str(failure)):
        tracker.remove_labels(1, iter(["ready", "running"]))
    row, = StateStore(tmp_path).local_issues()
    assert row["labels"] == ["blocked", "running"]
    assert run.calls == [
        (["api", "-X", "DELETE", "repos/owner/repo/issues/1/labels/ready"], None),
        (["api", "-X", "DELETE", "repos/owner/repo/issues/1/labels/running"], None),
    ]


@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("previously_merged", [False, True])
def test_recording_pr_observation_publishes_one_complete_update(tmp_path, monkeypatch, merged, previously_merged):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    remote = FakeTracker(now=lambda: NOW)
    remote.add_issue(1, "Feature")
    pr = remote.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Closes #1")
    if merged:
        remote.merge_pr(pr, expected_head_sha=remote.pr_head_sha(pr))
    store = StateStore(tmp_path, now=lambda: NOW)
    store.update_local_issue(1, pr_number=pr, merged=previously_merged)
    tracker = RecordingTracker(remote, SimpleNamespace(store=store, config=Config()))
    write = store._write_json
    observations = []

    def observe(path, value):
        write(path, value)
        observations.append(StateStore(tmp_path).local_issues()[0])

    monkeypatch.setattr(store, "_write_json", observe)
    tracker.list_prs("merged" if merged else "open", "foragent/")
    assert len(observations) == 1
    assert observations[0]["pr_number"] == pr
    assert observations[0]["pr_head"] == "foragent/issue-1"
    assert observations[0]["pr_base"] == "main" and observations[0]["merged"] is merged


def test_pr_renames_include_old_path_for_merge_policy():
    run = ScriptedRun('[{"filename":"src/new.py","previous_filename":"tests/acceptance/old.py"}]')
    assert GhTracker(REPO, run).pr_changed_files(42) == ["src/new.py", "tests/acceptance/old.py"]


class ScriptedRun:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, input=None):
        self.calls.append((args, input))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_fake_failed_logs_default_and_pr_isolation():
    tracker = FakeTracker()
    assert tracker.pr_failed_logs(42) == ""
    tracker.set_pr_failed_logs(42, "raw failure\n")
    assert tracker.pr_failed_logs(42) == "raw failure\n"
    assert tracker.pr_failed_logs(43) == ""


def test_gh_failed_logs_use_head_sha_all_pages_and_per_run_tails():
    head = "a" * 40
    runs = [
        {"id": 1, "conclusion": "failure"},
        {"id": 2, "conclusion": "success"},
        {"id": 3, "conclusion": None},
    ]
    second_page = [{"id": 4, "conclusion": "timed_out"}]
    run = ScriptedRun(
        json.dumps({"head": {"sha": head}}),
        json.dumps(runs) + "\n" + json.dumps(second_page),
        "discard me" + "A" * 4000,
        "discard me too" + "B" * 4000,
    )
    assert GhTracker(REPO, run).pr_failed_logs(42) == "A" * 4000 + "\n\n" + "B" * 4000
    assert run.calls == [
        (["api", "repos/owner/repo/pulls/42"], None),
        (["api", "--paginate", "--jq", ".workflow_runs",
          f"repos/owner/repo/actions/runs?head_sha={head}&per_page=100"], None),
        (["run", "view", "1", "-R", REPO, "--log-failed"], None),
        (["run", "view", "4", "-R", REPO, "--log-failed"], None),
    ]


def test_gh_failed_logs_empty_when_head_has_no_failed_actions():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":1,"conclusion":"success"}]')
    assert GhTracker(REPO, run).pr_failed_logs(42) == ""
    assert len(run.calls) == 2


def test_gh_failed_log_errors_propagate():
    run = ScriptedRun(
        '{"head":{"sha":"abc"}}', '[{"id":1,"conclusion":"failure"}]',
        subprocess.CalledProcessError(1, ["gh"], stderr="logs unavailable"),
    )
    with pytest.raises(TrackerError, match="logs unavailable"):
        GhTracker(REPO, run).pr_failed_logs(42)


def gh_error(stderr, stdout="", code=1):
    return subprocess.CalledProcessError(code, ["gh"], output=stdout, stderr=stderr)


@pytest.mark.parametrize("message,limited", [
    ("gh: API rate limit exceeded for user (HTTP 403)", True),
    ("GraphQL: API rate limit already exceeded for user", True),
    ("You have exceeded a secondary rate limit", True),
    ("gh: HTTP 429 Too Many Requests", True),
    ("gh: HTTP 403 Resource not accessible by integration", False),
    ("gh: HTTP 422 Validation Failed", False),
    ("request timed out", False),
])
def test_tracker_rate_limit_diagnostics_exclude_other_errors(message, limited):
    assert TrackerError(message).is_rate_limit is limited


def test_tracker_rate_limit_reset_header_survives_the_real_error_boundary():
    header = f"HTTP/2 403\r\nx-RaTeLiMiT-ReSeT: {int(NOW.timestamp())}\r\n\r\n"
    run = ScriptedRun(gh_error("API rate limit exceeded (HTTP 403)", header))
    with pytest.raises(TrackerError) as caught:
        GhTracker(REPO, run).get_issue(1)
    assert caught.value.is_rate_limit
    assert caught.value.reset_at == NOW
    assert len(run.calls) == 1


def test_tracker_rate_limit_probe_uses_injected_runner_and_only_exhausted_resources():
    run = ScriptedRun(json.dumps({"resources": {
        "core": {"remaining": 0, "reset": int(NOW.timestamp())},
        "graphql": {"remaining": 0, "reset": int((NOW + timedelta(minutes=5)).timestamp())},
        "search": {"remaining": 10, "reset": int((NOW + timedelta(hours=1)).timestamp())},
    }}))
    assert GhTracker(REPO, run).rate_limit_reset() == NOW + timedelta(minutes=5)
    assert run.calls == [(["api", "rate_limit"], None)]


def issue_json(number=1, **overrides):
    return {"number": number, "title": f"Issue {number}", "body": "body", "labels": [{"name": "approved"}], "state": "open", **overrides}


@pytest.mark.parametrize("already_approved", [False, True])
def test_strict_fake_rejects_approval_additions_before_any_mutation(already_approved):
    tracker = FakeTracker(strict=True, now=lambda: NOW)
    tracker.add_issue(1)
    if already_approved:
        tracker.record_label(1, "approved", "human")
    original = tracker.get_issue(1)
    with pytest.raises(AssertionError, match="must never add the approved label"):
        tracker.add_labels(1, iter(["ready", "approved"]))
    assert tracker.get_issue(1) == original
    tracker.add_labels(1, ["running"])
    assert "running" in tracker.get_issue(1).labels


def test_fake_body_edit_timestamp_tracks_only_body_changes():
    tracker = FakeTracker(now=lambda: NOW)
    tracker.add_issue(1, body="original")
    assert tracker.body_last_edited_at(1) is None
    tracker.add_labels(1, ["ready"])
    tracker.comment(1, "Comment")
    tracker.edit_body(1, "original")
    assert tracker.body_last_edited_at(1) is None
    tracker.edit_body(1, "edited")
    assert tracker.body_last_edited_at(1) == NOW.isoformat()
    tracker._now = lambda: NOW + timedelta(minutes=1)
    tracker.edit_body(1, "edited")
    assert tracker.body_last_edited_at(1) == NOW.isoformat()
    tracker.add_issue(1, body="replaced")
    assert tracker.body_last_edited_at(1) == (NOW + timedelta(minutes=1)).isoformat()


@pytest.mark.parametrize("edited_at", [None, "2026-10-20T09:01:00Z"])
def test_gh_body_last_edited_at_uses_graphql(edited_at):
    run = ScriptedRun(json.dumps({"data": {"repository": {"issue": {"lastEditedAt": edited_at}}}}))
    assert GhTracker(REPO, run).body_last_edited_at(42) == edited_at
    args, stdin = run.calls[0]
    assert args[:2] == ["api", "graphql"] and stdin is None
    assert "repository(owner: $owner, name: $name)" in args[3]
    assert "issue(number: $number) { lastEditedAt }" in args[3]
    assert args[4:] == ["-f", "owner=owner", "-f", "name=repo", "-F", "number=42"]


@pytest.mark.parametrize("response", [
    '{"errors": [{"message": "Forbidden"}]}',
    '{"data": {"repository": null}}',
    '{"data": {"repository": {"issue": {"lastEditedAt": 7}}}}',
    '[]', 'not JSON',
])
def test_gh_body_last_edited_at_errors_propagate(response):
    with pytest.raises(TrackerError, match="lastEditedAt"):
        GhTracker(REPO, ScriptedRun(response)).body_last_edited_at(42)


def test_gh_label_events_include_unapproval_and_reapproval_on_later_pages():
    def event(action, at, label="approved"):
        return {"event": action, "actor": {"login": "human"}, "label": {"name": label}, "created_at": at}

    first = [event("labeled", NOW.isoformat())] + [
        event("labeled", NOW.isoformat(), "feedback") for _ in range(99)
    ]
    second = [event("unlabeled", (NOW + timedelta(minutes=1)).isoformat())]
    third = [event("labeled", (NOW + timedelta(minutes=2)).isoformat())]
    run = ScriptedRun("\n".join(json.dumps(page) for page in (first, second, third)))
    events = GhTracker(REPO, run).label_events(1)
    assert len(events) == 102
    assert events[-2:] == [
        LabelEvent("approved", "unlabeled", "human", (NOW + timedelta(minutes=1)).isoformat()),
        LabelEvent("approved", "labeled", "human", (NOW + timedelta(minutes=2)).isoformat()),
    ]
    assert run.calls == [(["api", "--paginate", "repos/owner/repo/issues/1/events?per_page=100"], None)]


def test_gh_blocked_by_preserves_closure_reason():
    run = ScriptedRun(json.dumps([
        {"number": 1, "state": "closed", "state_reason": "not_planned"},
        {"number": 2, "state": "closed", "state_reason": "completed"},
        {"number": 3, "state": "open"},
    ]))
    assert GhTracker(REPO, run).blocked_by(4) == [
        Dependency(1, "closed", "not_planned"), Dependency(2, "closed", "completed"), Dependency(3, "open"),
    ]


def test_parent_and_paginated_sub_issues_gh():
    run = ScriptedRun('{"number": 99}', json.dumps([issue_json(1)]) + "\n" + json.dumps([issue_json(2, state="closed")]))
    tracker = GhTracker(REPO, run)
    assert tracker.parent_of(1) == 99
    assert [(issue.number, issue.state) for issue in tracker.sub_issues(99)] == [(1, "open"), (2, "closed")]
    assert run.calls == [
        (["api", "repos/owner/repo/issues/1/parent"], None),
        (["api", "--paginate", "repos/owner/repo/issues/99/sub_issues?per_page=100"], None),
    ]


@pytest.mark.parametrize("code", [404, 403, 500])
def test_only_missing_parent_is_none(code):
    tracker = GhTracker(REPO, ScriptedRun(gh_error(f"gh: error (HTTP {code})")))
    if code == 404:
        assert tracker.parent_of(1) is None
    else:
        with pytest.raises(TrackerError):
            tracker.parent_of(1)


def test_fake_sub_issues_and_dependencies_follow_child_closure():
    tracker = FakeTracker()
    tracker.add_issue(1)
    tracker.add_issue(2)
    tracker.add_issue(99)
    tracker.set_parent(1, 99)
    tracker.set_parent(2, 99)
    tracker.set_blocked_by(2, [Dependency(1, "open")])
    assert tracker.parent_of(1) == 99 and tracker.parent_of(99) is None
    tracker.close_issue(1)
    assert tracker.blocked_by(2) == [Dependency(1, "closed")]
    assert [(issue.number, issue.state) for issue in tracker.sub_issues(99)] == [(1, "closed"), (2, "open")]


def test_update_pr_sends_body_as_stdin_and_preserves_newlines():
    run = ScriptedRun("")
    GhTracker(REPO, run).update_pr(42, base="foragent/batch-99", title="Updated", body="## Review\nVERDICT: PASS\n")
    assert run.calls == [([
        "pr", "edit", "42", "-R", REPO, "--base", "foragent/batch-99", "--title", "Updated", "--body-file", "-",
    ], "## Review\nVERDICT: PASS\n")]


def test_fake_issues_dependencies_and_label_events():
    tracker = FakeTracker(now=lambda: NOW)
    assert tracker.add_issue(1, "First", "body", ["ready"]) == Issue(1, "First", "body", frozenset({"ready"}), "open")
    tracker.add_issue(2, state="closed")
    tracker.add_issue(3, is_pull_request=True)
    assert [issue.number for issue in tracker.list_open_issues()] == [1]
    assert tracker.blocked_by(1) == []
    deps = [Dependency(2, "closed"), Dependency(4, "open")]
    tracker.set_blocked_by(1, deps)
    deps.clear()
    assert tracker.blocked_by(1) == [Dependency(2, "closed"), Dependency(4, "open")]
    tracker.record_label(1, "approved", "human")
    tracker.add_labels(1, ["running", "running"])
    tracker.remove_labels(1, ["ready", "missing"])
    assert tracker.get_issue(1).labels == frozenset({"approved", "running"})
    assert tracker.label_events(1) == [
        LabelEvent("approved", "labeled", "human", NOW.isoformat()),
        LabelEvent("running", "labeled", "foragent-bot", NOW.isoformat()),
        LabelEvent("ready", "unlabeled", "foragent-bot", NOW.isoformat()),
    ]
    tracker.actor = "custom-bot"
    tracker.add_labels(1, ["needs-human"])
    assert tracker.label_events(1)[-1].actor == "custom-bot"
    tracker.remove_labels(1, ["needs-human"])
    assert tracker.label_events(1)[-1].actor == "custom-bot"


def test_fake_events_are_chronological_and_return_copies():
    dates = iter([NOW + timedelta(minutes=1), NOW])
    tracker = FakeTracker(now=lambda: next(dates))
    tracker.add_issue(1)
    tracker.record_label(1, "later", "human")
    tracker.record_label(1, "earlier", "human")
    events = tracker.label_events(1)
    assert [event.label for event in events] == ["earlier", "later"]
    events.clear()
    assert len(tracker.label_events(1)) == 2


def test_fake_labels_comments_and_closing():
    tracker = FakeTracker(actor="bot", now=lambda: NOW)
    tracker.add_issue(1)
    tracker.add_issue(2)
    tracker.ensure_labels([LabelSpec("ready", "ffffff", "Ready")])
    tracker.ensure_labels([LabelSpec("ready", "000000", "Updated")])
    assert tracker.labels == {"ready": LabelSpec("ready", "000000", "Updated")}
    assert tracker.list_comments(1) == []
    first = tracker.comment(1, "First\ncomment")
    second = tracker.comment(2, "Second comment")
    assert first != second
    assert tracker.list_comments(1) == [Comment(first, "bot", "First\ncomment", NOW.isoformat())]
    tracker.list_comments(1).clear()
    assert len(tracker.comments[1]) == 1
    tracker.close_issue(1, "Completed.")
    tracker.close_issue(2)
    assert tracker.get_issue(1).state == tracker.get_issue(2).state == "closed"
    assert [comment.body for comment in tracker.comments[1]] == ["First\ncomment", "Completed."]
    assert len(tracker.comments[2]) == 1


def test_fake_pr_creation_files_checks_and_merge():
    tracker = FakeTracker()
    tracker.add_issue(10)
    number = tracker.create_pr(head="feature", base="main", title="Change", body="Closes #10")
    assert number == 11
    assert asdict(tracker.created_prs[0]) == {
        "number": 11, "head": "feature", "base": "main", "title": "Change", "body": "Closes #10",
        "merged_at": None, "url": "",
    }
    assert [issue.number for issue in tracker.list_open_issues()] == [10]
    assert tracker.pr_checks(number) == "none"
    assert tracker.pr_changed_files(number) == []
    tracker.set_pr_files(number, ["src/a.py", "tests/acceptance/a.py"])
    assert tracker.pr_changed_files(number) == ["src/a.py", "tests/acceptance/a.py"]
    tracker.pr_changed_files(number).clear()
    assert tracker.pr_changed_files(number)
    for status in ("pending", "fail", "pass"):
        tracker.set_pr_checks(number, status)
        assert tracker.pr_checks(number) == status
    tracker.set_pr_head_sha(number, "a" * 40)
    assert tracker.pr_head_sha(number) == "a" * 40
    with pytest.raises(TrackerError, match="head changed"):
        tracker.merge_pr(number, expected_head_sha="b" * 40)
    assert tracker.merged_prs == [] and tracker.get_issue(number).state == "open"
    tracker.merge_pr(number, expected_head_sha="a" * 40)
    assert tracker.merged_prs == [number]
    assert tracker.get_issue(number).state == "closed"
    assert tracker.create_pr(head="next", base="main", title="Next", body="") == 12


def test_gh_list_and_get_issue_with_pagination_and_pr_filtering():
    run = ScriptedRun(
        json.dumps([issue_json(), issue_json(2, pull_request={})]) + "\n" + json.dumps([issue_json(3)]),
        json.dumps(issue_json(3, body=None, state="closed", labels=[])),
    )
    tracker = GhTracker(REPO, run)
    assert tracker.list_open_issues() == [
        Issue(1, "Issue 1", "body", frozenset({"approved"}), "open"),
        Issue(3, "Issue 3", "body", frozenset({"approved"}), "open"),
    ]
    assert tracker.get_issue(3) == Issue(3, "Issue 3", "", frozenset(), "closed")
    assert run.calls == [
        (["api", "--paginate", "repos/owner/repo/issues?state=open&per_page=100"], None),
        (["api", "repos/owner/repo/issues/3"], None),
    ]


def test_gh_label_events_and_dependencies():
    run = ScriptedRun(json.dumps([
        {"event": "unlabeled", "actor": {"login": "bot"}, "label": {"name": "ready"}, "created_at": "2026-10-20T10:00:00Z"},
        {"event": "closed"},
        {"event": "labeled", "actor": {"login": "human"}, "label": {"name": "approved"}, "created_at": "2026-10-20T09:00:00Z"},
    ]), json.dumps([{"number": 2, "state": "closed"}, {"number": 3, "state": "open"}]))
    tracker = GhTracker(REPO, run)
    assert tracker.label_events(1) == [
        LabelEvent("approved", "labeled", "human", "2026-10-20T09:00:00Z"),
        LabelEvent("ready", "unlabeled", "bot", "2026-10-20T10:00:00Z"),
    ]
    assert tracker.blocked_by(1) == [Dependency(2, "closed"), Dependency(3, "open")]
    assert run.calls == [
        (["api", "--paginate", "repos/owner/repo/issues/1/events?per_page=100"], None),
        (["api", "--paginate", "repos/owner/repo/issues/1/dependencies/blocked_by?per_page=100"], None),
    ]


def test_gh_dependencies_include_open_blockers_on_later_pages():
    closed = [{"number": n, "state": "closed"} for n in range(1, 101)]
    run = ScriptedRun(json.dumps(closed) + "\n" + json.dumps([{"number": 101, "state": "open"}]))
    dependencies = GhTracker(REPO, run).blocked_by(200)
    assert dependencies == [Dependency(n, "closed") for n in range(1, 101)] + [Dependency(101, "open")]
    assert run.calls == [
        (["api", "--paginate", "repos/owner/repo/issues/200/dependencies/blocked_by?per_page=100"], None),
    ]


def test_gh_add_remove_labels_and_ignore_only_404():
    run = ScriptedRun("{}", gh_error("gh: Not Found (HTTP 404)"), "", gh_error("gh: Forbidden (HTTP 403)"))
    tracker = GhTracker(REPO, run)
    tracker.add_labels(1, [])
    tracker.add_labels(1, ["ready", "risk:high"])
    tracker.remove_labels(1, ["space / 한글", "ready"])
    with pytest.raises(TrackerError, match="HTTP 403"):
        tracker.remove_labels(1, ["approved"])
    assert run.calls == [
        (["api", "-X", "POST", "repos/owner/repo/issues/1/labels", "-f", "labels[]=ready", "-f", "labels[]=risk:high"], None),
        (["api", "-X", "DELETE", "repos/owner/repo/issues/1/labels/space%20%2F%20%ED%95%9C%EA%B8%80"], None),
        (["api", "-X", "DELETE", "repos/owner/repo/issues/1/labels/ready"], None),
        (["api", "-X", "DELETE", "repos/owner/repo/issues/1/labels/approved"], None),
    ]


def test_gh_comments_use_json_stdin_and_list_pages():
    body = 'First line\n"quoted" café'
    run = ScriptedRun('{"id": 42}', json.dumps([
        {"id": 42, "user": {"login": "bot"}, "body": body, "created_at": NOW.isoformat()},
    ]) + "\n[]")
    tracker = GhTracker(REPO, run)
    assert tracker.comment(1, body) == 42
    assert tracker.list_comments(1) == [Comment(42, "bot", body, NOW.isoformat())]
    assert run.calls == [
        (["api", "-X", "POST", "repos/owner/repo/issues/1/comments", "--input", "-"], json.dumps({"body": body})),
        (["api", "--paginate", "repos/owner/repo/issues/1/comments?per_page=100"], None),
    ]


def test_gh_ensure_labels_creates_or_patches_on_422():
    run = ScriptedRun("{}", gh_error("gh: already_exists (HTTP 422)"), "{}")
    specs = [LabelSpec("ready", "00ff00", "Ready to run"), LabelSpec("risk:high /", "ff0000", "Review required")]
    GhTracker(REPO, run).ensure_labels(specs)
    first, second = [json.dumps(asdict(spec)) for spec in specs]
    assert run.calls == [
        (["api", "-X", "POST", "repos/owner/repo/labels", "--input", "-"], first),
        (["api", "-X", "POST", "repos/owner/repo/labels", "--input", "-"], second),
        (["api", "-X", "PATCH", "repos/owner/repo/labels/risk%3Ahigh%20%2F", "--input", "-"], second),
    ]


@pytest.mark.parametrize("responses", [
    [gh_error("HTTP 403 Forbidden")],
    [gh_error("HTTP 422 Already exists"), gh_error("HTTP 500 Server error")],
])
def test_gh_ensure_labels_other_errors_propagate(responses):
    with pytest.raises(TrackerError, match="HTTP"):
        GhTracker(REPO, ScriptedRun(*responses)).ensure_labels([LabelSpec("ready", "00ff00", "Ready")])


def test_gh_close_issue_with_optional_comment():
    run = ScriptedRun("", "")
    tracker = GhTracker(REPO, run)
    tracker.close_issue(1)
    tracker.close_issue(2, "Merged into batch.\nChecks passed.")
    assert run.calls == [
        (["issue", "close", "1", "-R", REPO], None),
        (["issue", "close", "2", "-R", REPO, "--comment", "Merged into batch.\nChecks passed."], None),
    ]


def test_gh_pr_create_files_and_merge():
    run = ScriptedRun("https://github.com/owner/repo/pull/42\n", '[{"filename": "src/a.py"}]\n[{"filename": "tests/a.py"}]', "")
    tracker = GhTracker(REPO, run)
    assert tracker.create_pr(head="feature", base="main", title="Title", body="Closes #1\nDetails") == 42
    assert tracker.pr_changed_files(42) == ["src/a.py", "tests/a.py"]
    tracker.merge_pr(42, expected_head_sha="a" * 40)
    assert run.calls == [
        (["pr", "create", "-R", REPO, "--head", "feature", "--base", "main", "--title", "Title", "--body-file", "-"], "Closes #1\nDetails"),
        (["api", "--paginate", "repos/owner/repo/pulls/42/files?per_page=100"], None),
        (["pr", "merge", "42", "-R", REPO, "--squash", "--delete-branch", "--match-head-commit", "a" * 40], None),
    ]


def test_gh_unparseable_pr_url_is_an_error():
    with pytest.raises(TrackerError, match="cannot parse PR number"):
        GhTracker(REPO, ScriptedRun("unexpected output")).create_pr(head="x", base="main", title="x", body="")


@pytest.mark.parametrize("method", ["merge", "rebase"])
def test_gh_alternate_merge_method(method):
    run = ScriptedRun("")
    GhTracker(REPO, run).merge_pr(42, method=method, expected_head_sha="a" * 40)
    assert run.calls == [([
        "pr", "merge", "42", "-R", REPO, f"--{method}", "--delete-branch",
        "--match-head-commit", "a" * 40,
    ], None)]


def test_gh_pr_head_sha_reads_current_head():
    run = ScriptedRun(json.dumps({"head": {"sha": "a" * 40}}))
    assert GhTracker(REPO, run).pr_head_sha(42) == "a" * 40
    assert run.calls == [(["api", "repos/owner/repo/pulls/42"], None)]


@pytest.mark.parametrize("buckets,expected", [
    ([], "none"), (["pass"], "pass"), (["skipping"], "pass"), (["pass", "skipping"], "pass"),
    (["pending"], "pending"), (["pass", "pending"], "pending"), (["fail"], "fail"),
    (["cancel"], "fail"), (["pass", "pending", "fail"], "fail"),
    (["cancel", "pending"], "fail"),
])
def test_gh_check_bucket_mapping(buckets, expected):
    run = ScriptedRun(json.dumps([{"bucket": bucket} for bucket in buckets]))
    assert GhTracker(REPO, run).pr_checks(42) == expected
    assert run.calls == [(["pr", "checks", "42", "-R", REPO, "--json", "bucket"], None)]


@pytest.mark.parametrize("response,expected", [
    (gh_error("no checks reported on the 'feature' branch"), "none"),
    (gh_error("", "no checks reported on the 'feature' branch"), "none"),
    (gh_error("", '[{"bucket": "fail"}]', 1), "fail"),
    (gh_error("", '[{"bucket": "pending"}]', 8), "pending"),
])
def test_gh_checks_expected_nonzero_exit(response, expected):
    assert GhTracker(REPO, ScriptedRun(response)).pr_checks(42) == expected


@pytest.mark.parametrize("response", [
    gh_error("HTTP 403 Forbidden"), gh_error("HTTP 500", '[{"bucket": "pass"}]', 2),
    "not json", '[{"bucket": "unknown"}]',
])
def test_gh_checks_unexpected_errors(response):
    with pytest.raises(TrackerError):
        GhTracker(REPO, ScriptedRun(response)).pr_checks(42)


@pytest.mark.parametrize("output", ["bad JSON", "{}", '[1]', '[]\ntrailing garbage'])
def test_gh_bad_paginated_json(output):
    with pytest.raises(TrackerError):
        GhTracker(REPO, ScriptedRun(output)).list_open_issues()


def test_gh_failures_include_stderr():
    message = "gh: authentication required (HTTP 401)"
    with pytest.raises(TrackerError) as error:
        GhTracker(REPO, ScriptedRun(gh_error(message))).get_issue(1)
    assert str(error.value) == message


def test_gh_missing_executable_is_wrapped():
    with pytest.raises(TrackerError, match="gh not found"):
        GhTracker(REPO, ScriptedRun(FileNotFoundError("gh not found"))).list_open_issues()


def test_default_runner_invokes_subprocess_without_a_shell(monkeypatch):
    calls = []

    def fake_subprocess(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='{"id": 5}')

    monkeypatch.setattr("foragent.tracker.subprocess.run", fake_subprocess)
    assert GhTracker(REPO).comment(1, "body") == 5
    assert calls == [(
        ["gh", "api", "-X", "POST", "repos/owner/repo/issues/1/comments", "--input", "-"],
        {"input": json.dumps({"body": "body"}), "check": True, "capture_output": True, "text": True,
         "timeout": 120},
    )]


def test_default_gh_timeout_is_wrapped(monkeypatch):
    def run(args, **kwargs):
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr("foragent.tracker.subprocess.run", run)
    with pytest.raises(TrackerError, match="timed out after 120 seconds") as error:
        GhTracker(REPO).list_prs()
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)


@pytest.mark.parametrize("comment", [None, "Closed by Foragent retry"])
def test_close_pr_deletes_branch_and_records_optional_comment(comment):
    fake = FakeTracker()
    pr = fake.create_pr(head="foragent/issue-1", base="main", title="PR", body="")
    fake.close_pr(pr, comment)
    assert fake.get_issue(pr).state == "closed"
    assert fake.deleted_branches == ["foragent/issue-1"]
    assert [item.body for item in fake.list_comments(pr)] == ([] if comment is None else [comment])
    assert fake.find_pr("foragent/issue-1") is None
    assert fake.list_prs("open") == fake.list_prs("merged") == []

    run = ScriptedRun("")
    GhTracker(REPO, run).close_pr(42, comment)
    args = ["pr", "close", "42", "-R", REPO, "--delete-branch"]
    if comment is not None:
        args.extend(["--comment", comment])
    assert run.calls == [(args, None)]


def test_create_issue_fake_and_gh_use_labels_and_json_body():
    body = 'Feedback\n"quoted" café $(literal)'
    tracker = FakeTracker(strict=True)
    first = tracker.create_issue("First", body, iter(["feedback"]))
    pr = tracker.create_pr(head="feature", base="main", title="PR", body="")
    last = tracker.create_issue("Last", "body", [])
    assert (first, pr, last) == (1, 2, 3)
    assert tracker.get_issue(first) == Issue(1, "First", body, frozenset({"feedback"}), "open")
    run = ScriptedRun('{"number": 42}')
    assert GhTracker(REPO, run).create_issue("First", body, iter(["feedback"])) == 42
    assert run.calls == [([
        "api", "-X", "POST", "repos/owner/repo/issues", "--input", "-",
    ], json.dumps({"title": "First", "body": body, "labels": ["feedback"]}))]


@pytest.mark.parametrize("state", ["open", "merged"])
def test_gh_list_prs_paginates_and_filters_heads_and_merges(state):
    def pull(number, head, merged_at=None, body="body"):
        return {
            "number": number, "head": {"ref": head}, "base": {"ref": "main"},
            "title": f"PR {number}", "body": body, "merged_at": merged_at,
            "html_url": f"https://github.com/owner/repo/pull/{number}",
        }
    merged_at = NOW.isoformat() if state == "merged" else None
    first = pull(10, "foragent/issue-1", merged_at)
    second = pull(20, "foragent/issue-2", merged_at, None)
    other = pull(30, "another/feature", merged_at)
    closed = [pull(40, "foragent/issue-4")] if state == "merged" else []
    run = ScriptedRun(json.dumps([first, other, *closed]) + "\n" + json.dumps([second]))
    assert GhTracker(REPO, run).list_prs(state=state, head_prefix="foragent/issue-") == [
        PullRequest(10, "foragent/issue-1", "main", "PR 10", "body", merged_at, first["html_url"]),
        PullRequest(20, "foragent/issue-2", "main", "PR 20", "", merged_at, second["html_url"]),
    ]
    api_state = "closed" if state == "merged" else "open"
    assert run.calls == [([
        "api", "--paginate", f"repos/owner/repo/pulls?state={api_state}&per_page=100",
    ], None)]


def test_fake_list_prs_separates_open_closed_and_merged():
    tracker = FakeTracker(now=lambda: NOW)
    opened = tracker.create_pr(head="foragent/issue-1", base="main", title="Open", body="")
    closed = tracker.create_pr(head="foragent/issue-2", base="main", title="Closed", body="")
    merged = tracker.create_pr(head="foragent/issue-3", base="main", title="Merged", body="")
    tracker.create_pr(head="feature", base="main", title="Other", body="")
    tracker.close_issue(closed)
    tracker.merge_pr(merged, expected_head_sha=tracker.pr_head_sha(merged))
    assert [pr.number for pr in tracker.list_prs("open", "foragent/issue-")] == [opened]
    merged_pr, = tracker.list_prs("merged", "foragent/issue-")
    assert merged_pr.number == merged and merged_pr.merged_at == NOW.isoformat()


@pytest.mark.parametrize("tracker", [FakeTracker(), GhTracker(REPO, ScriptedRun())])
def test_list_prs_rejects_invalid_state(tracker):
    with pytest.raises(ValueError, match="unsupported PR state"):
        tracker.list_prs("closed", "foragent/issue-")
@pytest.mark.parametrize("kind", ["sub", "blocked"])
def test_native_issue_edges_use_database_ids_and_ignore_duplicates(kind):
    calls = []

    def run(args, input=None):
        calls.append(args)
        if "POST" not in args:
            return '{"id":12345}'
        raise TrackerError("HTTP 422: relationship already exists")

    tracker = GhTracker("owner/repo", run=run)
    if kind == "sub":
        tracker.add_sub_issue(1, 2)
        endpoint, field = "repos/owner/repo/issues/1/sub_issues", "sub_issue_id=12345"
    else:
        tracker.add_blocked_by(1, 2)
        endpoint, field = "repos/owner/repo/issues/1/dependencies/blocked_by", "issue_id=12345"
    assert calls == [["api", "repos/owner/repo/issues/2"], ["api", "-X", "POST", endpoint, "-F", field]]


def test_remove_blocked_by_uses_database_id():
    run = ScriptedRun('{"id":12345}', "")
    GhTracker(REPO, run).remove_blocked_by(1, 2)
    assert run.calls == [
        (["api", "repos/owner/repo/issues/2"], None),
        (["api", "-X", "DELETE", "repos/owner/repo/issues/1/dependencies/blocked_by/12345"], None),
    ]


def test_remove_blocked_by_fake_preserves_other_dependencies_and_is_repeatable():
    tracker = FakeTracker()
    tracker.set_blocked_by(1, [Dependency(2, "open"), Dependency(3, "closed")])
    tracker.remove_blocked_by(1, 2)
    tracker.remove_blocked_by(1, 2)
    tracker.remove_blocked_by(4, 2)
    assert tracker.blocked_by(1) == [Dependency(3, "closed")]
    assert tracker.blocked_by(4) == []


def test_remove_blocked_by_propagates_api_failure():
    run = ScriptedRun('{"id":12345}', gh_error("HTTP 403: forbidden"))
    with pytest.raises(TrackerError, match="HTTP 403"):
        GhTracker(REPO, run).remove_blocked_by(1, 2)
