from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import subprocess

import pytest

from foragent.tracker import (
    Comment, Dependency, FakeTracker, GhTracker, Issue, LabelEvent, LabelSpec, TrackerError,
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


def gh_error(stderr, stdout="", code=1):
    return subprocess.CalledProcessError(code, ["gh"], output=stdout, stderr=stderr)


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
    assert asdict(tracker.created_prs[0]) == {"number": 11, "head": "feature", "base": "main", "title": "Change", "body": "Closes #10"}
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
        {"input": json.dumps({"body": "body"}), "check": True, "capture_output": True, "text": True},
    )]
