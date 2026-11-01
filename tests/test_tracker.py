from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest

from test_scheduler import ctx as scheduler_context  # noqa: F401

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


def test_outbox_repeated_limit_keeps_fifo_and_acknowledges_partial_label_removal(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    clock = [NOW]
    first_reset, second_reset = NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)
    limited = lambda reset: gh_error("API rate limit exceeded (HTTP 403)",
                                    f"X-RateLimit-Reset: {int(reset.timestamp())}\r\n")
    run = ScriptedRun(limited(first_reset), "[]", "[]", limited(second_reset),
                      "[]", '{"id": 88}', "")
    store = StateStore(tmp_path, now=lambda: clock[0])
    issue = Issue(1, "Feature", "body", frozenset({"blocked", "running"}), "open")
    store.record_issue(issue, {})
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: clock[0])
    tracker = RecordingTracker(GhTracker(REPO, run), ctx, queue_writes=True)
    ctx.tracker = tracker
    tracker.add_labels(1, ["ready"])
    tracker.remove_labels(1, ["blocked", "running"])
    tracker.comment(1, "Pending evidence")
    tracker.close_issue(1)
    assert len(run.calls) == 1
    assert store.local_issues()[0]["state"] == "closed"

    clock[0] = first_reset
    tracker.flush_outbox()
    pending = StateStore(tmp_path).github_outbox()
    assert [entry["action"] for entry in pending] == ["remove_labels", "comment", "close_issue"]
    assert pending[0]["labels"] == ["running"]
    assert store.github_wait_until(clock[0]) == second_reset
    partial = replace(issue, labels=frozenset({"ready", "running"}), updated_at=first_reset.isoformat())
    assert store.refresh_local_issues([partial], [], {}, clock[0]) == []
    assert store.local_issues()[0]["state"] == "closed"

    clock[0] = second_reset
    ctx.store = StateStore(tmp_path, now=lambda: clock[0])
    tracker.flush_outbox()
    assert ctx.store.github_outbox() == []
    assert ctx.store.local_issues()[0]["comments"][0]["id"] == 88
    assert len(run.calls) == 7
    assert sum(args[-1].endswith("/labels/blocked") for args, _ in run.calls) == 1


def test_outbox_flush_propagates_validation_error_without_discarding_pending_write(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    store = StateStore(tmp_path, now=lambda: NOW)
    entry = store.queue_github_write(1, "comment", body="Evidence")
    run = ScriptedRun(gh_error("HTTP 422 Validation Failed"))
    tracker = RecordingTracker(GhTracker(REPO, run), SimpleNamespace(store=store, config=Config()))
    with pytest.raises(TrackerError, match="422"):
        tracker.flush_outbox()
    assert store.github_outbox() == [entry]
    assert store.github_wait_until(NOW) is None


@pytest.fixture
def outbox_context(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    clock = [NOW + timedelta(milliseconds=100)]
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(1, "Feature", "body")
    ctx = SimpleNamespace(remote=remote, clock=clock, config=Config(), now=lambda: clock[0],
                          store=StateStore(tmp_path, now=lambda: clock[0]),
                          reset=NOW + timedelta(minutes=1))
    ctx.tracker = RecordingTracker(remote, ctx, queue_writes=True)
    ctx.tracker.refresh_issues()
    ctx.store.pause_github(ctx.reset)
    return ctx


@pytest.mark.parametrize("deleted_at", ["refresh", "comments", "flush"])
def test_outbox_missing_issue_drops_all_its_writes_and_delivers_fifo_tail(
    outbox_context, monkeypatch, deleted_at,
):
    from foragent import orch

    ctx = outbox_context
    ctx.remote.add_issue(2, "Surviving issue")
    ctx.tracker.refresh_issues()
    ctx.tracker.comment(1, "Deleted issue evidence")
    ctx.tracker.comment(2, "Surviving evidence")
    ctx.tracker.close_issue(1)
    ctx.clock[0] = ctx.reset
    attempts = []
    get_issue, comment = ctx.remote.get_issue, ctx.remote.comment
    list_comments = ctx.remote.list_comments

    def get(number):
        if number not in ctx.remote.issues:
            raise TrackerError("gh: Not Found (HTTP 404)")
        return get_issue(number)

    def write(number, body):
        attempts.append(number)
        get(number)
        return comment(number, body)

    def read_comments(number):
        if deleted_at == "comments" and number == 1:
            del ctx.remote.issues[1]
        get(number)
        return list_comments(number)

    monkeypatch.setattr(ctx.remote, "get_issue", get)
    monkeypatch.setattr(ctx.remote, "comment", write)
    monkeypatch.setattr(ctx.remote, "list_comments", read_comments)
    if deleted_at == "flush":
        ctx.tracker.refresh_issues()
    if deleted_at != "comments":
        del ctx.remote.issues[1]
    if deleted_at != "flush":
        ctx.tracker.refresh_issues()
    ctx.tracker.flush_outbox()
    ctx.tracker.refresh_issues()
    ctx.tracker.flush_outbox()

    assert attempts == ([1, 2] if deleted_at == "flush" else [2])
    assert ctx.store.github_outbox() == []
    assert ctx.store._issue_snapshot().get("outbox_base", {}) == {}
    assert [row["number"] for row in ctx.store.local_issues()] == [2]
    assert [item.body for item in ctx.remote.list_comments(2)] == ["Surviving evidence"]
    event, = orch.log(ctx)
    assert (event["kind"], event["issue"]) == ("outbox-dropped", 1)
    assert "issue not found" in event["summary"]


def test_outbox_write_404_does_not_drop_an_existing_issue(outbox_context, monkeypatch):
    ctx = outbox_context
    ctx.tracker.comment(1, "Pending evidence")
    pending = ctx.store.github_outbox()

    def missing_resource(*args):
        raise TrackerError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(ctx.remote, "comment", missing_resource)
    with pytest.raises(TrackerError, match="404"):
        ctx.tracker.flush_outbox()
    assert ctx.store.github_outbox() == pending
    assert ctx.store.local_issues()[0]["number"] == 1


@pytest.mark.parametrize("limited", [False, True])
def test_outbox_failed_missing_issue_lookup_preserves_pending_writes(outbox_context, monkeypatch, limited):
    ctx = outbox_context
    ctx.tracker.comment(1, "Pending evidence")
    pending = ctx.store.github_outbox()
    ctx.clock[0] = ctx.reset
    until = ctx.reset + timedelta(minutes=1)

    def missing_resource(*args):
        raise TrackerError("gh: Not Found (HTTP 404)")

    def unavailable(*args):
        raise TrackerError("API rate limit exceeded (HTTP 403)" if limited else "HTTP 503",
                           stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")

    monkeypatch.setattr(ctx.remote, "comment", missing_resource)
    monkeypatch.setattr(ctx.remote, "get_issue", unavailable)
    if limited:
        ctx.tracker.flush_outbox()
        assert ctx.store.github_wait_until(ctx.clock[0]) == until
    else:
        with pytest.raises(TrackerError, match="503"):
            ctx.tracker.flush_outbox()
    assert ctx.store.github_outbox() == pending
    assert ctx.store.local_issues()[0]["number"] == 1


def test_batch_completion_uses_pending_child_close(outbox_context, git_history):
    from conftest import git_command
    from foragent.git import GitRepo
    from foragent.runner import _batch_pr

    ctx = outbox_context
    ctx.repo = "owner/repo"
    ctx.git = GitRepo(git_history.clone)
    base = git_history.tip
    git_history.update({"feature.txt": "done\n"})
    git_command(git_history.origin, "update-ref", "refs/heads/foragent/batch-2", git_history.tip)
    git_command(git_history.origin, "update-ref", "refs/heads/main", base)
    ctx.remote.add_issue(2, "Batch parent")
    ctx.remote.set_parent(1, 2)
    ctx.tracker.refresh_issues()
    ctx.tracker.close_issue(1)
    assert ctx.remote.get_issue(1).state == "open"

    _batch_pr(2, ctx)

    assert [pr.head for pr in ctx.remote.created_prs] == ["foragent/batch-2"]
    assert ctx.remote.get_issue(1).state == "open"


@pytest.mark.parametrize("change", ["labels", "state"])
def test_outbox_refresh_drops_human_label_or_state_change_in_same_second(outbox_context, change):
    from foragent import orch

    ctx = outbox_context
    ctx.tracker.add_labels(1, ["ready"])
    ctx.clock[0] = NOW + timedelta(milliseconds=900)
    value = {"labels": frozenset({"needs-human"}), "state": "closed"}[change]
    ctx.remote.issues[1] = replace(ctx.remote.get_issue(1), **{change: value})
    ctx.remote.issues[1] = replace(ctx.remote.get_issue(1), updated_at=NOW.isoformat())
    human = ctx.remote.get_issue(1)
    ctx.clock[0] = ctx.reset
    ctx.tracker.refresh_issues()
    ctx.tracker.flush_outbox()

    assert ctx.store.github_outbox() == []
    assert ctx.remote.get_issue(1) == human
    row, = ctx.store.local_issues()
    assert row["labels"] == sorted(human.labels)
    assert (row["title"], row["body"], row["state"]) == (human.title, human.body, human.state)
    assert [event["kind"] for event in orch.log(ctx)] == ["outbox-dropped"]


@pytest.mark.parametrize("change", ["title", "body", "comments"])
def test_outbox_refresh_keeps_label_and_close_writes_after_human_content_change(outbox_context, change):
    from foragent import orch

    ctx = outbox_context
    ctx.tracker.add_labels(1, ["blocked"])
    ctx.tracker.close_issue(1)
    ctx.clock[0] = NOW + timedelta(milliseconds=900)
    if change == "comments":
        ctx.remote.comment(1, "Human evidence")
    else:
        ctx.remote.issues[1] = replace(ctx.remote.get_issue(1), **{change: f"Human {change}"})
    human = ctx.remote.get_issue(1)
    ctx.clock[0] = ctx.reset
    ctx.tracker.refresh_issues()
    assert len(ctx.store.github_outbox()) == 2

    ctx.tracker.flush_outbox()
    ctx.tracker.refresh_issues()

    assert ctx.store.github_outbox() == []
    current = ctx.remote.get_issue(1)
    assert (current.title, current.body) == (human.title, human.body)
    assert current.labels == {"blocked"} and current.state == "closed"
    assert [comment.body for comment in ctx.remote.list_comments(1)] == (
        ["Human evidence"] if change == "comments" else []
    )
    row, = ctx.store.local_issues()
    assert (row["title"], row["body"], row["labels"], row["state"]) == (
        human.title, human.body, ["blocked"], "closed",
    )
    assert orch.log(ctx) == []


def test_outbox_partial_delivery_does_not_hide_human_label_before_ack(outbox_context, monkeypatch):
    from foragent import orch

    ctx = outbox_context
    ctx.tracker.comment(1, "Foragent evidence")
    ctx.tracker.remove_labels(1, ["blocked"])
    comment = ctx.remote.comment
    next_reset = ctx.reset + timedelta(minutes=1)

    def interleaved(number, body):
        identity = comment(number, body)
        ctx.clock[0] += timedelta(seconds=1)
        ctx.remote.record_label(number, "blocked", "human")
        ctx.remote.issues[number] = replace(ctx.remote.get_issue(number), updated_at=ctx.clock[0].isoformat())
        ctx.clock[0] += timedelta(seconds=1)
        return identity

    def limited(*args):
        raise TrackerError("API rate limit exceeded (HTTP 403)",
                           stdout=f"X-RateLimit-Reset: {int(next_reset.timestamp())}\r\n")

    ctx.clock[0] = ctx.reset
    with monkeypatch.context() as patch:
        patch.setattr(ctx.remote, "comment", interleaved)
        patch.setattr(ctx.remote, "remove_labels", limited)
        ctx.tracker.flush_outbox()
    assert [entry["action"] for entry in ctx.store.github_outbox()] == ["remove_labels"]
    assert ctx.store.local_issues()[0]["comments"][0]["id"] > 0

    ctx.clock[0] = next_reset
    ctx.tracker.refresh_issues()
    ctx.tracker.flush_outbox()
    assert ctx.store.github_outbox() == []
    assert ctx.remote.get_issue(1).labels == frozenset({"blocked"})
    assert ctx.store.local_issues()[0]["labels"] == ["blocked"]
    assert len(ctx.remote.list_comments(1)) == 1
    assert [event["kind"] for event in orch.log(ctx)] == ["outbox-dropped"]


@pytest.mark.parametrize("action", ["add_labels", "remove_labels", "comment", "close_issue", "update_issue"])
def test_outbox_existing_issue_queue_serializes_default_tracker_writes(outbox_context, action):
    from foragent import orch
    from foragent.tracker import RecordingTracker

    ctx = outbox_context
    ctx.tracker.add_labels(1, ["ready"])
    original = ctx.remote.get_issue(1)
    ctx.clock[0] = ctx.reset
    writer = RecordingTracker(ctx.remote, ctx)
    if action == "update_issue":
        writer.update_issue(1, title="New title", body="New body")
    else:
        payload = {"add_labels": ["blocked"], "remove_labels": ["ready"],
                   "comment": "Runner evidence", "close_issue": "Done"}[action]
        getattr(writer, action)(1, payload)

    assert ctx.remote.get_issue(1) == original
    assert ctx.remote.list_comments(1) == []
    assert [entry["action"] for entry in ctx.store.github_outbox()] == ["add_labels", action]
    ctx.tracker.refresh_issues()
    assert len(ctx.store.github_outbox()) == 2
    ctx.tracker.flush_outbox()
    ctx.tracker.refresh_issues()
    assert ctx.store.github_outbox() == []
    assert orch.log(ctx) == []
    row, = ctx.store.local_issues()
    assert row["labels"] == sorted(ctx.remote.get_issue(1).labels)
    assert [item["body"] for item in row["comments"]] == [item.body for item in ctx.remote.list_comments(1)]


def test_outbox_enqueue_publishes_local_intent_atomically(outbox_context, monkeypatch):
    ctx = outbox_context
    queue = ctx.store.queue_github_write
    snapshots = []

    def interleaved(*args, **kwargs):
        entry = queue(*args, **kwargs)
        snapshots.append(ctx.store.local_issues()[0]["labels"])
        if entry["action"] == "add_labels":
            ctx.tracker.remove_labels(1, ["ready"])
        return entry

    monkeypatch.setattr(ctx.store, "queue_github_write", interleaved)
    ctx.tracker.add_labels(1, ["ready"])
    assert snapshots == [["ready"], []]
    assert ctx.store.local_issues()[0]["labels"] == []
    ctx.clock[0] = ctx.reset
    ctx.tracker.refresh_issues()
    ctx.tracker.flush_outbox()
    assert ctx.remote.get_issue(1).labels == frozenset()
    assert ctx.store.local_issues()[0]["labels"] == []


def test_outbox_refresh_ignores_timestamp_changes_without_content_changes(outbox_context):
    from foragent import orch

    ctx = outbox_context
    ctx.tracker.add_labels(1, ["ready"])
    ctx.remote.issues[1] = replace(ctx.remote.get_issue(1), updated_at=(ctx.reset + timedelta(hours=1)).isoformat())
    ctx.clock[0] = ctx.reset
    ctx.tracker.refresh_issues()
    assert len(ctx.store.github_outbox()) == 1
    assert orch.log(ctx) == []
    ctx.tracker.flush_outbox()
    assert ctx.remote.get_issue(1).labels == frozenset({"ready"})


@pytest.mark.parametrize("action", ["comment", "close_issue", "update_issue"])
def test_outbox_partial_delivery_advances_content_baseline_across_processes(outbox_context, monkeypatch, action):
    from foragent import orch
    from foragent.state import StateStore

    ctx = outbox_context
    if action == "update_issue":
        ctx.tracker.update_issue(1, title="New title", body="New body")
    else:
        getattr(ctx.tracker, action)(1, "Foragent evidence")
    ctx.tracker.add_labels(1, ["ready"])
    next_reset = ctx.reset + timedelta(minutes=1)

    def limited(*args):
        raise TrackerError("API rate limit exceeded (HTTP 403)",
                           stdout=f"X-RateLimit-Reset: {int(next_reset.timestamp())}\r\n")

    ctx.clock[0] = ctx.reset
    ctx.tracker.refresh_issues()
    with monkeypatch.context() as patch:
        patch.setattr(ctx.remote, "add_labels", limited)
        ctx.tracker.flush_outbox()
    assert [entry["action"] for entry in ctx.store.github_outbox()] == ["add_labels"]

    ctx.store = StateStore(ctx.store.root, now=ctx.now)
    ctx.clock[0] = next_reset
    ctx.tracker.refresh_issues()
    assert len(ctx.store.github_outbox()) == 1
    assert orch.log(ctx) == []
    ctx.tracker.flush_outbox()
    assert ctx.store.github_outbox() == []
    assert ctx.remote.get_issue(1).labels == frozenset({"ready"})
    assert len(ctx.remote.list_comments(1)) == (action != "update_issue")


def test_outbox_delivery_preserves_concurrent_append(outbox_context, monkeypatch):
    ctx = outbox_context
    ctx.tracker.add_labels(1, ["ready"])
    add = ctx.remote.add_labels

    def append(number, labels):
        ctx.tracker.comment(number, "Appended during delivery")
        return add(number, labels)

    ctx.clock[0] = ctx.reset
    with monkeypatch.context() as patch:
        patch.setattr(ctx.remote, "add_labels", append)
        ctx.tracker.flush_outbox()
    assert [entry["action"] for entry in ctx.store.github_outbox()] == ["comment"]
    ctx.tracker.refresh_issues()
    ctx.tracker.flush_outbox()
    assert ctx.store.github_outbox() == []
    assert [item.body for item in ctx.remote.list_comments(1)] == ["Appended during delivery"]
    assert ctx.store.local_issues()[0]["comments"][0]["id"] > 0


def test_outbox_other_issue_does_not_defer_default_tracker_write(outbox_context):
    from foragent.tracker import RecordingTracker

    ctx = outbox_context
    ctx.tracker.add_labels(1, ["ready"])
    ctx.remote.add_issue(2, "Other issue")
    writer = RecordingTracker(ctx.remote, ctx)
    writer.get_issue(2)
    writer.comment(2, "Independent evidence")
    assert [entry["issue"] for entry in ctx.store.github_outbox()] == [1]
    assert [item.body for item in ctx.remote.list_comments(2)] == ["Independent evidence"]


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


STARTED_STEPS = json.dumps([{"name": "Run tests", "status": "completed", "conclusion": "failure"}])


def failed_job(number, name="test", steps=STARTED_STEPS):
    return json.dumps([{"id": number, "name": name, "conclusion": "failure",
                        "steps": json.loads(steps), "html_url": f"https://job/{number}"}])


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
        failed_job(11), "[]", "discard me" + "A" * 4000,
        "[]",  # The unfinished run 3 has no failed job yet.
        failed_job(14), "[]", "discard me too" + "B" * 4000,
    )
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "A" * 4000 in logs and "B" * 4000 in logs
    assert "discard me" not in logs
    assert logs.index("Actions run 1") < logs.index("A" * 4000) < logs.index("Actions run 4")
    assert not logs.not_started
    assert run.calls == [
        (["api", "repos/owner/repo/pulls/42"], None),
        (["api", "--paginate", "--jq", ".workflow_runs",
          f"repos/owner/repo/actions/runs?head_sha={head}&per_page=100"], None),
        (["api", "--paginate", "--jq", ".jobs", "repos/owner/repo/actions/runs/1/jobs?per_page=100"], None),
        (["api", "--paginate", "repos/owner/repo/check-runs/11/annotations"], None),
        (["run", "view", "1", "-R", REPO, "--job", "11", "--log-failed"], None),
        (["api", "--paginate", "--jq", ".jobs", "repos/owner/repo/actions/runs/3/jobs?per_page=100"], None),
        (["api", "--paginate", "--jq", ".jobs", "repos/owner/repo/actions/runs/4/jobs?per_page=100"], None),
        (["api", "--paginate", "repos/owner/repo/check-runs/14/annotations"], None),
        (["run", "view", "4", "-R", REPO, "--job", "14", "--log-failed"], None),
    ]


def test_gh_failed_logs_empty_when_head_has_no_failed_actions():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":1,"conclusion":"success"}]')
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert logs == "" and not logs.not_started
    assert len(run.calls) == 2


def test_gh_failed_log_errors_propagate():
    limited = gh_error("gh: API rate limit exceeded for user (HTTP 403)")
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":1,"conclusion":"failure"}]', failed_job(11), "[]", limited)
    with pytest.raises(TrackerError, match="rate limit"):
        GhTracker(REPO, run).pr_failed_logs(42)


def test_gh_failed_log_read_error_becomes_a_notice_and_keeps_annotation():
    run = ScriptedRun(
        '{"head":{"sha":"abc"}}', '[{"id":1,"conclusion":"failure"}]', failed_job(11),
        json.dumps([{"message": "assert failed"}]), gh_error("HTTP 500 boom"),
    )
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: log of job test: HTTP 500 boom" in logs
    assert "assert failed" in logs and "Job test failed" in logs and not logs.not_started


@pytest.mark.parametrize("steps,annotation", [
    ("[]", "A runner could not be provisioned."),
    (json.dumps([{"name": "Run tests", "status": "queued", "conclusion": None}]),
     "The job was not started because the spending limit needs to be increased."),
])
def test_gh_failed_logs_mark_jobs_that_never_started_without_reading_logs(steps, annotation):
    run = ScriptedRun(
        '{"head":{"sha":"abc"}}', '[{"id":7,"name":"ci","conclusion":"failure","html_url":"https://run/7"}]',
        failed_job(70, steps=steps), json.dumps([{"message": annotation}]),
    )
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert logs.not_started
    assert annotation in logs and "Actions run 7" in logs and "Job test did not start" in logs
    assert not any("--log-failed" in call[0] for call in run.calls)


def test_gh_failed_logs_mixed_jobs_are_a_code_failure_and_keep_both():
    jobs = json.loads(failed_job(70, "build", steps="[]")) + json.loads(failed_job(71, "unit"))
    run = ScriptedRun(
        '{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"failure"}]', json.dumps(jobs),
        json.dumps([{"message": "The job was not started."}]), "[]", "assert 1 == 2",
    )
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert not logs.not_started
    assert "The job was not started." in logs and "Job unit failed" in logs and "assert 1 == 2" in logs


def test_gh_failed_logs_missing_log_of_started_job_is_kept_as_evidence():
    run = ScriptedRun(
        '{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"failure"}]', failed_job(70), "[]",
        subprocess.CalledProcessError(1, ["gh"], stderr="log not found: 70"),
    )
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert not logs.not_started and "log not found: 70" in logs


def test_gh_failed_run_without_failed_jobs_is_a_code_failure():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"startup_failure"}]', "[]",
                      "invalid workflow file")
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert not logs.not_started and "invalid workflow file" in logs


IN_PROGRESS = '[{"id":7,"name":"ci","status":"in_progress","conclusion":null}]'
JOBS_7 = "repos/owner/repo/actions/runs/7/jobs?per_page=100"


def pending_job(number=79):
    return {"id": number, "name": "pending", "status": "in_progress", "conclusion": None, "steps": []}


def test_gh_failed_logs_read_every_failed_job_of_an_unfinished_run_while_siblings_pend(tmp_path):
    jobs = (json.loads(failed_job(70, "unit")) + json.loads(failed_job(71, "acceptance (0)"))
            + [pending_job()])
    run = ScriptedRun(
        '{"head":{"sha":"abc"}}', IN_PROGRESS, json.dumps(jobs),
        json.dumps([{"message": "unit broke"}]), "first" + "A" * 4000,
        json.dumps([{"message": "lost ids"}]), "second" + "B" * 4000,
    )
    logs = GhTracker(REPO, run).pr_failed_logs(42, full_text_dir=tmp_path)
    assert not logs.not_started and "Actions run 7 (ci) in_progress" in logs
    assert "A" * 4000 in logs and "B" * 4000 in logs, "One job's output must not hide another's"
    assert "firstA" not in logs and "secondB" not in logs
    assert logs.index("Job unit failed") < logs.index("unit broke") < logs.index("A" * 4000)
    assert logs.index("A" * 4000) < logs.index("Job acceptance (0) failed") < logs.index("lost ids")
    assert (tmp_path / "ci-job-70.log").read_text().startswith("first")
    assert (tmp_path / "ci-job-71.log").read_text().startswith("second")
    assert [call[0] for call in run.calls[-4:]] == [
        ["api", "--paginate", "repos/owner/repo/check-runs/70/annotations"],
        ["api", "--allow-escape-sequences", "repos/owner/repo/actions/jobs/70/logs"],
        ["api", "--paginate", "repos/owner/repo/check-runs/71/annotations"],
        ["api", "--allow-escape-sequences", "repos/owner/repo/actions/jobs/71/logs"],
    ]


def test_gh_failed_job_log_removes_terminal_control_and_keeps_the_text():
    raw = "\ufeff\x1b[36;1muv run pytest\x1b[0m\r\n\x1b]8;;https://evil\x07link\x1b]8;;\x1b\\\tFAILED tests/a.py::b\x07\x1b\n"
    run = ScriptedRun('{"head":{"sha":"abc"}}', IN_PROGRESS, failed_job(70), "[]", raw)
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "uv run pytest\nlink\tFAILED tests/a.py::b\n" in logs
    assert "\x1b" not in logs and "\x07" not in logs and "\r" not in logs and "evil" not in logs


def test_gh_log_of_only_terminal_control_is_empty():
    run = ScriptedRun('{"head":{"sha":"abc"}}', IN_PROGRESS, failed_job(70), "[]", "\x1b[0m\n")
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "GitHub returned an empty log" in logs


def test_gh_unfinished_run_without_failed_jobs_adds_nothing():
    run = ScriptedRun('{"head":{"sha":"abc"}}', IN_PROGRESS, json.dumps([pending_job()]))
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert logs == "" and not logs.not_started


def test_gh_not_started_job_of_an_unfinished_run_stays_not_started_without_log_reads():
    jobs = json.loads(failed_job(70, steps="[]")) + [pending_job()]
    run = ScriptedRun('{"head":{"sha":"abc"}}', IN_PROGRESS, json.dumps(jobs),
                      json.dumps([{"message": "A runner could not be provisioned."}]))
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert logs.not_started and "A runner could not be provisioned." in logs
    assert "Job test did not start" in logs and len(run.calls) == 4


def test_gh_not_started_in_one_run_is_not_erased_by_a_pending_run():
    runs = '[{"id":6,"conclusion":"failure"},{"id":7,"status":"queued","conclusion":null}]'
    run = ScriptedRun('{"head":{"sha":"abc"}}', runs, failed_job(60, steps="[]"), "[]", json.dumps([pending_job()]))
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert logs.not_started and "Actions run 7" not in logs


def test_gh_runs_unreadable_is_a_notice_that_proves_nothing():
    run = ScriptedRun('{"head":{"sha":"abc"}}', gh_error("HTTP 502 runs"))
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: workflow runs: HTTP 502 runs" in logs and not logs.not_started


def test_gh_unreadable_changed_files_keep_measurement_runs_out_of_the_evidence():
    # Failed logs no longer read changed files: measurement runs are ordinary CI evidence.
    from foragent.ci_workflow import WORKFLOW

    runs = [{"id": 6, "path": WORKFLOW, "conclusion": "failure"},
            {"id": 7, "path": ".github/workflows/ci.yml", "conclusion": "failure"}]
    run = ScriptedRun('{"head":{"sha":"abc"}}', json.dumps(runs), failed_job(60, "acceptance (0)"), "[]", "lost ids",
                      failed_job(70), "[]", "ordinary tail")
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "unavailable" not in logs and "Actions run 6" in logs and not logs.not_started
    assert logs.index("lost ids") < logs.index("ordinary tail")
    assert all("/files" not in call[0][-1] for call in run.calls)


def test_gh_unreadable_jobs_of_a_completed_run_keep_its_run_wide_log():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"failure"}]',
                      gh_error("HTTP 403 jobs"), "run-wide tail")
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: jobs of run 7: HTTP 403 jobs" in logs
    assert "run-wide tail" in logs and not logs.not_started
    assert run.calls[-1][0] == ["run", "view", "7", "-R", REPO, "--log-failed"]


def test_gh_unreadable_jobs_of_an_unfinished_run_do_not_prove_not_started_or_read_refused_logs():
    run = ScriptedRun('{"head":{"sha":"abc"}}', IN_PROGRESS, gh_error("HTTP 403 jobs"))
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: jobs of run 7: HTTP 403 jobs" in logs and not logs.not_started
    assert run.calls[-1][0][-1] == JOBS_7


def test_gh_unreadable_annotations_keep_the_log_and_prove_nothing():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"failure"}]', failed_job(70),
                      gh_error("HTTP 403 annotations"), "failing test id")
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: annotations of job test: HTTP 403 annotations" in logs
    assert "failing test id" in logs and "Job test failed" in logs and not logs.not_started


def test_gh_job_without_steps_stays_not_started_when_only_its_annotations_are_unreadable():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"failure"}]',
                      failed_job(70, steps="[]"), gh_error("HTTP 403 annotations"))
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert logs.not_started and "CI evidence unavailable: annotations of job test" in logs


@pytest.mark.parametrize("runs", ['[{"id":7,"conclusion":"failure"}]', IN_PROGRESS])
@pytest.mark.parametrize("empty", ["", " \n"])
def test_gh_empty_job_log_is_a_notice_not_a_blank(runs, empty):
    run = ScriptedRun('{"head":{"sha":"abc"}}', runs, failed_job(70), "[]", empty)
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: log of job test: GitHub returned an empty log" in logs
    assert "Job test failed" in logs and not logs.not_started


def test_gh_empty_run_wide_log_is_a_notice_not_a_blank():
    run = ScriptedRun('{"head":{"sha":"abc"}}', '[{"id":7,"conclusion":"startup_failure"}]', "[]", "")
    logs = GhTracker(REPO, run).pr_failed_logs(42)
    assert "CI evidence unavailable: failed log of run 7: GitHub returned an empty log" in logs
    assert not logs.not_started


@pytest.mark.parametrize("before", [
    pytest.param([], id="runs"),
    pytest.param(['[{"id":7,"path":"%s","conclusion":"failure"}]'], id="changed-files"),
    pytest.param(['[{"id":7,"conclusion":"failure"}]'], id="jobs"),
    pytest.param(['[{"id":7,"conclusion":"failure"}]', failed_job(70)], id="annotations"),
    pytest.param(['[{"id":7,"conclusion":"failure"}]', failed_job(70), "[]"], id="job-log"),
    pytest.param(['[{"id":7,"conclusion":"null"}]'.replace('"null"', "null"), failed_job(70), "[]"], id="rest-log"),
    pytest.param(['[{"id":7,"conclusion":"startup_failure"}]', "[]"], id="run-log"),
])
def test_gh_rate_limit_on_every_evidence_read_propagates(before):
    from foragent.ci_workflow import WORKFLOW

    before = [item.replace("%s", WORKFLOW) for item in before]
    run = ScriptedRun('{"head":{"sha":"abc"}}', *before, gh_error("gh: API rate limit exceeded for user (HTTP 403)"))
    with pytest.raises(TrackerError) as error:
        GhTracker(REPO, run).pr_failed_logs(42)
    assert error.value.is_rate_limit


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


def test_parent_issue_gh_reads_parent_body_in_one_call():
    run = ScriptedRun(json.dumps(issue_json(99, body="Shared outcome.")))
    parent = GhTracker(REPO, run).parent_issue(1)
    assert (parent.number, parent.body) == (99, "Shared outcome.")
    assert run.calls == [(["api", "repos/owner/repo/issues/1/parent"], None)]
    assert GhTracker(REPO, ScriptedRun(gh_error("gh: error (HTTP 404)"))).parent_issue(1) is None


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
    run = ScriptedRun("https://github.com/owner/repo/pull/42\n", '[{"filename": "src/a.py"}]\n[{"filename": "tests/a.py"}]',
                      json.dumps({"title": "Title (#1)"}), "")
    tracker = GhTracker(REPO, run)
    assert tracker.create_pr(head="feature", base="main", title="Title", body="Closes #1\nDetails") == 42
    assert tracker.pr_changed_files(42) == ["src/a.py", "tests/a.py"]
    tracker.merge_pr(42, expected_head_sha="a" * 40)
    assert run.calls == [
        (["pr", "create", "-R", REPO, "--head", "feature", "--base", "main", "--title", "Title", "--body-file", "-"], "Closes #1\nDetails"),
        (["api", "--paginate", "repos/owner/repo/pulls/42/files?per_page=100"], None),
        (["pr", "view", "42", "-R", REPO, "--json", "title"], None),
        (["pr", "merge", "42", "-R", REPO, "--squash", "--delete-branch", "--match-head-commit", "a" * 40,
          "--subject", "Title (#1)", "--body", ""], None),
    ]


def test_gh_squash_merge_uses_an_explicit_subject_without_reading_the_title():
    run = ScriptedRun("")
    GhTracker(REPO, run).merge_pr(42, expected_head_sha="a" * 40, subject="Feature (#1)")
    assert run.calls == [([
        "pr", "merge", "42", "-R", REPO, "--squash", "--delete-branch",
        "--match-head-commit", "a" * 40, "--subject", "Feature (#1)", "--body", "",
    ], None)]


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
@pytest.mark.parametrize('link', [None, 'https://github.com/owner/repo/actions/runs/42'])
def test_gh_check_bucket_mapping(buckets, expected, link):
    run = ScriptedRun(json.dumps([{"bucket": bucket, "link": link} for bucket in buckets]))
    assert GhTracker(REPO, run).pr_checks(42) == expected
    assert run.calls == [(["pr", "checks", "42", "-R", REPO, "--json", "bucket,name,workflow,link"], None)]


@pytest.mark.parametrize("response,expected", [
    (gh_error("no checks reported on the 'feature' branch"), "none"),
    (gh_error("", "no checks reported on the 'feature' branch"), "none"),
    (gh_error("", '[{"bucket": "fail"}]', 1), "fail"),
    (gh_error("", '[{"bucket": "pending"}]', 8), "pending"),
])
def test_gh_checks_expected_nonzero_exit(response, expected):
    assert GhTracker(REPO, ScriptedRun(response)).pr_checks(42) == expected


@pytest.mark.parametrize('changed', [[], [{'filename': '.github/workflows/ci.yml'}],
    [{'filename': '.github/workflows/foragent-acceptance.yml'}],
    [{'filename': '.github/workflows/nested/custom.yaml'}],
    [{'filename': 'moved.yml', 'previous_filename': '.github/workflows/old.yml'}]])
@pytest.mark.parametrize('unit_bucket', ['pass', 'fail'])
@pytest.mark.parametrize('path', ['.github/workflows/foragent-acceptance.yml',
    '.github/workflows/foragent-acceptance.yml@refs/heads/main',
    '.github/workflows/foragent-acceptance-publish.yml'])
def test_renamed_unit_workflow_cannot_hide_failed_checks(changed, unit_bucket, path):
    # A failed acceptance job is red whichever workflow or path it claims.
    checks = [
        {'bucket': 'fail', 'name': 'acceptance (0)', 'workflow': 'foragent-acceptance',
         'link': f'https://github.com/{REPO}/actions/runs/1/job/100'},
        {'bucket': unit_bucket, 'name': 'foragent-acceptance/0', 'workflow': 'foragent-acceptance',
         'link': f'https://github.com/{REPO}/actions/runs/2/job/101'},
    ]
    github = ScriptedRun(gh_error('', json.dumps(checks), 1))
    assert GhTracker(REPO, github).pr_checks(42) == 'fail'
    assert len(github.calls) == 1, 'Status comes from the checks alone, not workflow provenance'


@pytest.mark.parametrize('external_status', [False, True])
@pytest.mark.parametrize('damage', [None, 'missing-suite', 'foreign-repo', 'wrong-path',
                                  'wrong-suite', 'non-actions', 'api-failure', 'workflow-change', 'colliding-check'])
def test_commit_status_cannot_impersonate_acceptance_job(external_status, damage):
    # Every failed row counts, with or without verifiable provenance.
    link = f'https://github.com/{REPO}/actions/runs/42/job/100'
    checks = [{'bucket': 'fail', 'name': 'acceptance (0)', 'workflow': 'foragent-acceptance', 'link': link}]
    if external_status:
        # gh's StatusContext row: target_url may duplicate every display field,
        # but it has no CheckSuite/WorkflowRun and therefore no workflow value.
        checks.append({'bucket': 'fail', 'name': 'acceptance (0)', 'workflow': '', 'link': link})
    if damage == 'colliding-check':
        checks.append(dict(checks[0]))
    github = ScriptedRun(gh_error('', json.dumps(checks), 1))

    assert GhTracker(REPO, github).pr_checks(42) == 'fail'
    assert len(github.calls) == 1


@pytest.mark.parametrize('ordinary,expected', [(None, 'none'), ('pass', 'pass'), ('pending', 'pending'),
                                              ('fail', 'fail'), ('cancel', 'fail')])
@pytest.mark.parametrize('acceptance', ['fail', 'pending'])
def test_ordinary_ci_excludes_acceptance_jobs_reports_and_failed_logs(ordinary, expected, acceptance):
    # Acceptance jobs join ordinary CI: their state and failed logs count like any other job's.
    from foragent.ci_workflow import WORKFLOW

    checks = [
        {'name': 'acceptance (0)', 'workflow': 'custom measurement name', 'bucket': acceptance,
         'link': 'https://github.com/owner/repo/actions/runs/1/job/10'},
    ]
    if ordinary is not None:
        checks.append({'name': 'unit', 'workflow': 'ci', 'bucket': ordinary})
    ordinary_failed = expected == 'fail'
    combined = 'fail' if acceptance == 'fail' or ordinary_failed else 'pending'
    response = gh_error('', json.dumps(checks), 1 if combined == 'fail' else 8)
    runs = [{'id': 1, 'name': 'custom measurement name', 'path': WORKFLOW,
             'conclusion': 'failure' if acceptance == 'fail' else None,
             'html_url': 'https://github.com/owner/repo/actions/runs/1'}]
    if ordinary_failed:
        runs.append({'id': 3, 'name': 'ci', 'path': '.github/workflows/ci.yml', 'conclusion': 'failure'})
    responses = [response, '{"head":{"sha":"head"}}', json.dumps(runs)]
    if acceptance == 'fail':
        responses.extend([failed_job(10, 'acceptance (0)'), '[]', 'lost test ids'])
    else:
        responses.append(json.dumps([pending_job(10)]))
    if ordinary_failed:
        responses.extend([failed_job(30), '[]', 'ordinary CI failure'])
    run = ScriptedRun(*responses)
    tracker = GhTracker(REPO, run)
    assert tracker.pr_checks(42) == combined
    logs = tracker.pr_failed_logs(42)
    assert ('lost test ids' in logs) is (acceptance == 'fail') and not logs.not_started
    assert ('ordinary CI failure' in logs) is ordinary_failed
    assert not run.responses


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
        {"input": json.dumps({"body": "body"}).encode(), "check": True, "capture_output": True,
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
def test_close_pr_deletes_branch_and_records_optional_comment(comment, monkeypatch):
    fake = FakeTracker()
    pr = fake.create_pr(head="foragent/issue-1", base="main", title="PR", body="")
    issue_closes = []
    close_issue = fake.close_issue

    def observe_issue_close(n, comment=None):
        issue_closes.append(n)
        close_issue(n, comment)

    monkeypatch.setattr(fake, "close_issue", observe_issue_close)
    fake.close_pr(pr, comment)
    assert issue_closes == []
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


@pytest.fixture
def local_records(tmp_path, monkeypatch):
    from foragent import tracker as tracker_module
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(tracker_module, "_EMPTY_RETRY_DELAY", 0)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: store))
    return store


@pytest.mark.parametrize(("local_state", "state", "raises"), [
    ("open", "open", True),
    ("closed", "open", False),
    ("closed", "all", True),
    (None, "all", False),
])
def test_empty_listing_checks_local_issue_records(local_records, local_state, state, raises):
    if local_state is not None:
        local_records.update_local_issue(1, state=local_state)
    calls = []
    tracker = GhTracker("owner/repo", run=lambda args, input=None: calls.append(args) or "[]")
    if raises:
        with pytest.raises(TrackerError, match="empty listing"):
            tracker.list_issues(state)
    else:
        assert tracker.list_issues(state) == []
    assert len(calls) == 2


def test_empty_open_listing_after_nonempty_all_listing_is_trusted(local_records):
    replies = [json.dumps([issue_json(1, state="closed")]), "[]", "[]"]
    tracker = GhTracker("owner/repo", run=lambda args, input=None: replies.pop(0))
    assert [issue.number for issue in tracker.list_issues("all")] == [1]
    assert tracker.list_open_issues() == [], "Every issue may be closed"


def test_empty_open_listing_after_all_listing_with_open_issue_raises(local_records):
    still_open = json.dumps([issue_json(1, state="open")])
    replies = [still_open, "[]", "[]", still_open]
    tracker = GhTracker("owner/repo", run=lambda args, input=None: replies.pop(0))
    assert [issue.number for issue in tracker.list_issues("all")] == [1]
    with pytest.raises(TrackerError, match="empty listing"):
        tracker.list_open_issues()
    assert replies == [], "The earlier sighting is checked against a fresh full listing"


def test_empty_open_listing_is_trusted_when_earlier_open_issue_was_closed_since(local_records):
    calls = []
    replies = [json.dumps([issue_json(1, state="open")]), "[]", "[]",
               json.dumps([issue_json(1, state="closed")]), "[]", "[]"]

    def run(args, input=None):
        calls.append(args[-1])
        return replies.pop(0)

    tracker = GhTracker("owner/repo", run=run)
    assert [issue.number for issue in tracker.list_open_issues()] == [1]
    assert tracker.list_open_issues() == [], "Closing the last open issue is not a GitHub hiccup"
    assert "state=all" in calls[3]
    assert tracker.list_open_issues() == [], "Once settled, the old sighting is not checked again"
    assert replies == []


def test_fake_advances_updated_at_on_each_issue_change():
    clock = [NOW]
    tracker = FakeTracker(now=lambda: clock[0])
    tracker.add_issue(1)
    tracker.add_issue(2)
    for change in (
        lambda: tracker.record_label(1, "approved", "human"),
        lambda: tracker.remove_labels(1, ["approved"]),
        lambda: tracker.comment(1, "Note"),
        lambda: tracker.edit_body(1, "New body"),
        lambda: tracker.add_blocked_by(1, 2),
        lambda: tracker.set_blocked_by(1, []),
        lambda: tracker.set_parent(1, 2),
        lambda: tracker.close_issue(1),
    ):
        clock[0] += timedelta(seconds=1)
        change()
        assert tracker.get_issue(1).updated_at == clock[0].isoformat()
    assert tracker.get_issue(2).updated_at == (NOW + timedelta(seconds=7)).isoformat(), "A new child changes its parent"


def test_refresh_reads_per_issue_facts_only_for_changed_issues_and_dependents_of_changed_blockers(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    clock = [NOW]
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(1, "Dependent", labels=["approved"])
    remote.add_issue(2, "Approved alone", labels=["approved"])
    remote.add_issue(3, "Blocker")
    remote.add_issue(4, "Waiting", labels=["blocked"])
    remote.add_blocked_by(1, 3)
    ctx = SimpleNamespace(remote=remote, config=Config(), now=lambda: clock[0],
                          store=StateStore(tmp_path, now=lambda: clock[0]))
    tracker = RecordingTracker(remote, ctx)
    tracker.refresh_issues()
    reads = []
    for name in ("list_comments", "sub_issues", "blocked_by"):
        def counted(n, _name=name, _original=getattr(remote, name)):
            reads.append((_name, n))
            return _original(n)
        setattr(remote, name, counted)

    clock[0] += timedelta(minutes=5)
    tracker.refresh_issues()
    assert reads == [], "An unchanged listing needs no per-issue reads"

    clock[0] += timedelta(minutes=5)
    remote.close_issue(3)
    tracker.refresh_issues()
    assert reads == [("blocked_by", 1)], "Only the closed blocker's dependent is read again"
    rows = {row["number"]: row for row in ctx.store.local_issues()}
    assert rows[1]["blocked_by"] == [{"number": 3, "state": "closed", "state_reason": None}]

    reads.clear()
    clock[0] += timedelta(minutes=5)
    remote.comment(4, "A person answered")
    tracker.refresh_issues()
    assert sorted(reads) == [("list_comments", 4), ("sub_issues", 4)]
    assert [comment["body"] for comment in ctx.store.local_issues()[3]["comments"]] == ["A person answered"]


def test_gh_issue_list_keeps_link_counts_and_blockers_name_other_repositories():
    data = issue_json(1, sub_issues_summary={"total": 2, "completed": 1, "percent_completed": 50},
                      issue_dependencies_summary={"blocked_by": 1, "total_blocked_by": 2,
                                                  "blocking": 0, "total_blocking": 0})
    issue, = GhTracker(REPO, ScriptedRun(json.dumps([data]))).list_issues()
    assert dict(issue.sub_issues_summary) == {"total": 2, "completed": 1, "percent_completed": 50}
    assert dict(issue.dependencies_summary)["blocked_by"] == 1
    run = ScriptedRun(json.dumps([
        {"number": 1, "state": "open", "repository_url": "https://api.github.com/repos/owner/repo"},
        {"number": 2, "state": "closed", "repository_url": "https://api.github.com/repos/other/lib"},
    ]))
    assert GhTracker(REPO, run).blocked_by(4) == [Dependency(1, "open"), Dependency(2, "closed", None, "other/lib")]


def test_refresh_rereads_links_when_only_a_link_count_changes(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    clock = [NOW]
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(1, "Dependent", labels=["approved"])
    remote.add_issue(2, "Blocker")
    remote.add_blocked_by(1, 2)
    remote.issues[1] = replace(remote.issues[1], dependencies_summary=(("blocked_by", 1), ("total_blocked_by", 1)))
    ctx = SimpleNamespace(config=Config(), now=lambda: clock[0], store=StateStore(tmp_path, now=lambda: clock[0]))
    tracker = RecordingTracker(remote, ctx)
    tracker.refresh_issues()
    # GitHub may not advance updated_at for a removed link; the link count still changes.
    remote._dependencies[1] = []
    remote.issues[1] = replace(remote.issues[1], dependencies_summary=(("blocked_by", 0), ("total_blocked_by", 0)))
    clock[0] += timedelta(minutes=5)
    tracker.refresh_issues()
    assert ctx.store.local_issues()[0]["blocked_by"] == []


def test_merged_prs_updated_since_stops_at_the_first_page_older_than_the_cutoff():
    def pull(number, at, head="foragent/issue-1", merged=True):
        stamp = at.isoformat().replace("+00:00", "Z")
        return {"number": number, "head": {"ref": head}, "base": {"ref": "main"},
                "html_url": f"https://github.com/owner/repo/pull/{number}",
                "merged_at": stamp if merged else None, "updated_at": stamp}

    # A page whose times are unknown cannot prove it is old, so reading continues.
    first = [pull(index, NOW, head="other") for index in range(99)] + [
        {**pull(99, NOW), "updated_at": None}]
    second = [pull(200, NOW - timedelta(minutes=1)), pull(201, NOW, merged=False),
              *(pull(300 + index, NOW - timedelta(days=1)) for index in range(98))]
    run = ScriptedRun(json.dumps(first), json.dumps(second))
    records = GhTracker(REPO, run).merged_prs_updated_since(NOW - timedelta(hours=1), "foragent/")
    assert [record["number"] for record in records] == [99, 200, *range(300, 398)]
    assert [args[1] for args, _ in run.calls] == [
        f"repos/owner/repo/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}"
        for page in (1, 2)]

    # Without a cutoff, reading stops only at a short page.
    run = ScriptedRun(json.dumps(second), json.dumps([]))
    assert len(GhTracker(REPO, run).merged_prs_updated_since(None)) == 99
    assert len(run.calls) == 2


def test_merged_prs_updated_since_accepts_pulls_without_updated_at():
    item = {"number": 5, "head": {"ref": "foragent/issue-5"}, "base": {"ref": "main"},
            "html_url": "https://github.com/owner/repo/pull/5", "merged_at": NOW.isoformat()}
    run = ScriptedRun(json.dumps([item]))
    assert GhTracker(REPO, run).merged_prs_updated_since(NOW, "foragent/") == [{
        "number": 5, "head": "foragent/issue-5", "base": "main", "merged_at": NOW.isoformat(),
        "url": item["html_url"], "updated_at": None}]


def test_gh_attempts_count_only_with_a_recorder_and_include_failures(tmp_path):
    from foragent import gh
    from foragent.state import StateStore

    store = StateStore(tmp_path, now=lambda: datetime(2026, 10, 20, 9, 30, tzinfo=UTC))
    attempts = []

    def run(args, input=None):
        attempts.append(args)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="boom")
        return "[]"

    tracker = GhTracker("owner/repo", run=run, sleep=lambda seconds: None)
    with pytest.raises(TrackerError):
        tracker.list_open_issues()
    assert store.github_calls() == []  # No recorder: nothing counts.
    gh.set_recorder(store, lambda: datetime(2026, 10, 20, 9, 45, tzinfo=UTC))
    tracker.list_open_issues()  # An empty listing is retried once: two attempts.
    assert store.github_calls() == [{"hour": "2026-10-20T09:00:00+00:00", "calls": 2}]


def test_recording_tracker_installs_its_context_as_the_gh_recorder(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from foragent import gh
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    store = StateStore(tmp_path)
    when = datetime(2026, 10, 20, 11, tzinfo=UTC)
    RecordingTracker(FakeTracker(), SimpleNamespace(store=store, now=lambda: when))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
    gh.run(["auth", "status"], capture_output=True, check=False)
    assert calls == [["gh", "auth", "status"]]
    assert store.github_calls() == [{"hour": "2026-10-20T11:00:00+00:00", "calls": 1}]


def test_gh_issues_closed_since_is_one_filtered_listing_without_pull_requests():
    items = [{"number": 4, "title": "t", "body": "", "labels": [], "state": "closed"},
             {"number": 5, "title": "p", "body": "", "labels": [], "state": "closed", "pull_request": {}}]
    calls = []
    tracker = GhTracker(REPO, run=lambda args, input=None: calls.append(args) or json.dumps(items))
    assert [issue.number for issue in tracker.issues_closed_since(NOW)] == [4]
    assert calls == [["api", "--paginate",
                      "repos/owner/repo/issues?state=closed&since=2026-10-20T09%3A00%3A00Z&per_page=100"]]


def test_observe_closed_issues_starts_at_the_last_listing_and_records_unlisted_closes(tmp_path):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    clock = [NOW]
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(1, "Work", "body")
    remote.add_issue(2, "Other", "body")
    ctx = SimpleNamespace(config=Config(), now=lambda: clock[0],
                          store=StateStore(tmp_path, now=lambda: clock[0]))
    tracker = RecordingTracker(remote, ctx)

    def states():
        return {row["number"]: row["state"] for row in ctx.store.local_issues()}

    # Without a listing or a watermark there is no span to read yet.
    tracker.observe_closed_issues(observed_at=clock[0])
    assert states() == {} and ctx.store.closed_issue_watermark() == clock[0]

    # Upgrading with a cached listing: the first read starts at that listing.
    (tmp_path / "closed_issues.json").unlink()
    tracker.refresh_issues()
    clock[0] += timedelta(minutes=1)
    remote.close_issue(1)
    remote.add_issue(3, "Created and closed between listings", "body", state="closed")
    tracker.observe_closed_issues(observed_at=clock[0])
    assert states() == {1: "closed", 2: "open", 3: "closed"}
    assert ctx.store.closed_issue_watermark() == clock[0]


def test_bounded_gh_caps_each_call_and_starts_none_once_the_shared_budget_is_spent(monkeypatch):
    import subprocess
    import time

    from foragent import gh
    from foragent.tracker import GhTracker

    clock, timeouts = [100.0], []

    def run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 3
        return subprocess.CompletedProcess(argv, 0, stdout=b"[]")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    tracker = GhTracker("owner/repo")
    budget = gh.Budget(5, 4)
    with budget.spend():
        tracker.sub_issues(1)  # 4 s cap of 5 left.
    with budget.spend():
        tracker.sub_issues(2)  # Only 2 s are left.
        with pytest.raises(TrackerError, match="deferred"):
            tracker.sub_issues(3)  # Nothing left: no subprocess at all.
    tracker.sub_issues(4)  # Outside a bounded block the usual timeout applies.
    assert timeouts == [4, 2, 120]


def test_close_not_planned_gh_and_fake():
    run = ScriptedRun("")
    GhTracker(REPO, run).close_issue(41, "Replaced by #42", reason="not_planned")
    assert run.calls == [(["issue", "close", "41", "-R", REPO,
                           "--reason", "not planned", "--comment", "Replaced by #42"], None)]
    tracker = FakeTracker()
    tracker.add_issue(41, "Old work", "body")
    tracker.close_issue(41, "Replaced by #42", reason="not_planned")
    assert tracker.get_issue(41).state == "closed"
    assert tracker.get_issue(41).state_reason == "not_planned"
    assert [c.body for c in tracker.list_comments(41)] == ["Replaced by #42"]


def test_close_not_planned_survives_recording_and_outbox(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    store = StateStore(tmp_path, now=lambda: NOW)
    remote = FakeTracker()
    remote.add_issue(41, "Old work", "body")
    store.record_issue(remote.get_issue(41), {})
    tracker = RecordingTracker(remote, SimpleNamespace(store=store, config=Config(), now=lambda: NOW))
    # Force the same FIFO route that a rate-limited close takes, without an external service.
    store.queue_github_write(41, "close_issue", body="Replaced by #42", reason="not_planned")
    assert store.local_issues()[0]["state_reason"] == "not_planned"
    tracker.flush_outbox()
    assert store.github_outbox() == []
    assert remote.get_issue(41).state_reason == "not_planned"
    assert [c.body for c in remote.list_comments(41)] == ["Replaced by #42"]
    remote.add_issue(42, "Other old work", "body")
    store.record_issue(remote.get_issue(42), {})
    tracker.close_issue(42, reason="not_planned")
    assert next(row for row in store.local_issues() if row["number"] == 42)["state_reason"] == "not_planned"


def _replacement_tracker(tmp_path, labels=()):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    store = StateStore(tmp_path, now=lambda: NOW)
    remote = FakeTracker()
    remote.add_issue(41, "Old work", "body", list(labels))
    store.record_issue(remote.get_issue(41), {})
    return store, remote, RecordingTracker(remote, SimpleNamespace(store=store, config=Config(), now=lambda: NOW))


def _replace_41(tracker):
    from foragent import order_replaces

    order_replaces.close_replaced({"id": "order", "issues": []}, {41: ["new"]}, tracker, {"new": 7}, {7}, "en")


def test_replacement_behind_queued_writes_closes_and_comments_once(tmp_path):
    store, remote, tracker = _replacement_tracker(tmp_path)
    store.queue_github_write(41, "add_labels", labels=["later"])
    for _ in range(2):  # Republishing must not repeat anything, delivered or not.
        _replace_41(tracker)
    # The closure never waits in the outbox, where a later flush could act on stale facts.
    assert not [entry for entry in store.github_outbox() if entry["action"] in ("close_issue", "comment")]
    assert remote.get_issue(41).state_reason == "not_planned"
    assert len(remote.list_comments(41)) == 1 and "#7" in remote.list_comments(41)[0].body
    tracker.flush_outbox()
    tracker.flush_outbox()
    assert len(remote.list_comments(41)) == 1


def test_replacement_comment_sent_but_reported_failed_is_not_retried_by_a_flush(tmp_path, monkeypatch):
    store, remote, tracker = _replacement_tracker(tmp_path)
    store.queue_github_write(41, "add_labels", labels=["later"])
    post = remote.comment

    def sent_then_failed(number, body):
        post(number, body)
        raise TrackerError("connection lost after the post")

    monkeypatch.setattr(remote, "comment", sent_then_failed)
    with pytest.raises(TrackerError):
        _replace_41(tracker)
    monkeypatch.setattr(remote, "comment", post)
    tracker.flush_outbox()
    _replace_41(tracker)
    tracker.flush_outbox()
    assert len(remote.list_comments(41)) == 1
    assert not [entry for entry in store.github_outbox() if entry["action"] == "comment"]


def test_replacement_rate_limit_stays_out_of_the_outbox(tmp_path, monkeypatch):
    store, remote, tracker = _replacement_tracker(tmp_path)
    tracker.queue_writes = True
    close = remote.close_issue

    def limited(*args, **kwargs):
        raise TrackerError("HTTP 403 API rate limit exceeded")

    monkeypatch.setattr(remote, "close_issue", limited)
    with pytest.raises(TrackerError):
        _replace_41(tracker)
    assert store.github_outbox() == []
    monkeypatch.setattr(remote, "close_issue", close)
    _replace_41(tracker)
    assert remote.get_issue(41).state_reason == "not_planned" and len(remote.list_comments(41)) == 1


def test_replacement_leaves_a_close_already_waiting_to_be_sent(tmp_path):
    store, remote, tracker = _replacement_tracker(tmp_path)
    store.queue_github_write(41, "close_issue", body=None, reason="completed")
    _replace_41(tracker)
    assert remote.get_issue(41).state == "open" and remote.list_comments(41) == []
    assert [entry["reason"] for entry in store.github_outbox()] == ["completed"]


def test_replacement_sees_a_running_label_still_waiting_to_be_sent(tmp_path):
    store, remote, tracker = _replacement_tracker(tmp_path)
    store.queue_github_write(41, "add_labels", labels=["running"])
    with pytest.raises(ValueError, match="#41.*running"):
        _replace_41(tracker)
    assert remote.get_issue(41).state == "open"


def test_replacement_sees_a_run_on_github_behind_unsent_writes(tmp_path):
    from foragent import order_replaces

    store, remote, tracker = _replacement_tracker(tmp_path)
    store.queue_github_write(41, "add_labels", labels=["later"])
    remote.add_labels(41, ["running"])  # The local view of the issue still shows it idle.
    plan = {"id": "order", "issues": [{"key": "new", "replaces": [41]}]}
    with pytest.raises(ValueError, match="#41.*running"):
        order_replaces.plan_closures(plan, tracker, None, {}, "en")
    with pytest.raises(ValueError, match="#41.*running"):
        _replace_41(tracker)
    assert remote.get_issue(41).state == "open" and remote.list_comments(41) == []
    tracker.flush_outbox()
    assert remote.get_issue(41).state == "open"


def test_replacement_trusts_github_over_an_unsent_label_removal(tmp_path):
    store, remote, tracker = _replacement_tracker(tmp_path, ["running"])
    store.queue_github_write(41, "remove_labels", labels=["running"])
    assert "running" not in tracker.pending_issue(41).labels
    with pytest.raises(ValueError, match="#41.*running"):
        _replace_41(tracker)
    assert remote.get_issue(41).state == "open"


def test_pending_close_reason_waits_for_the_matching_observation(tmp_path):
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import RecordingTracker

    clock = [NOW]
    store = StateStore(tmp_path, now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(41, "Old work", "body", state="closed")
    remote.issues[41] = replace(remote.get_issue(41), state_reason="completed")
    tracker = RecordingTracker(remote, SimpleNamespace(store=store, config=Config(), now=lambda: clock[0]))
    tracker.get_issue(41)
    store.queue_github_write(41, "close_issue", body=None, reason="not_planned")
    clock[0] += timedelta(seconds=1)
    tracker.get_issue(41)
    # Merely seeing closed does not confirm the requested close reason was delivered.
    assert [entry["reason"] for entry in store.github_outbox()] == ["not_planned"]
    assert store.local_issues()[0]["state_reason"] == "not_planned"
    clock[0] += timedelta(seconds=1)
    tracker.flush_outbox()
    clock[0] += timedelta(seconds=1)
    assert tracker.get_issue(41).state_reason == "not_planned"
    assert store.github_outbox() == []


@pytest.mark.parametrize("error", ["HTTP 403 Issue is locked", "HTTP 422 Body is too long"])
def test_refused_evidence_does_not_block_workflow_or_later_evidence(outbox_context, monkeypatch, error):
    ctx = outbox_context
    from foragent import orch_advice
    assert orch_advice.publish(ctx.tracker, ctx, 1, "Refused exact\n\n advice") is None
    refused, = ctx.store.github_outbox()
    ctx.store.queue_github_write(1, "comment", body="Later evidence", evidence=True)
    ctx.store.queue_github_write(1, "add_labels", labels=["ready"])
    ctx.clock[0] = ctx.reset
    post = ctx.remote.comment
    attempts = []

    def comment(number, body):
        attempts.append(body)
        if body == refused["body"]:
            raise TrackerError(error)
        return post(number, body)

    monkeypatch.setattr(ctx.remote, "comment", comment)
    ctx.tracker.flush_outbox()
    ctx.tracker.flush_outbox()
    assert attempts == [refused["body"], "Later evidence", refused["body"]]
    assert ctx.store.github_outbox() == [refused]
    assert ctx.store.github_blocking_outbox() == []
    assert [comment.body for comment in ctx.remote.list_comments(1)] == ["Later evidence"]
    assert "ready" in ctx.remote.get_issue(1).labels
    ctx.tracker.add_labels(1, ["running"])
    assert "running" in ctx.remote.get_issue(1).labels
    monkeypatch.setattr(ctx.remote, "comment", post)
    ctx.tracker.flush_outbox()
    ctx.tracker.flush_outbox()
    assert [comment.body for comment in ctx.remote.list_comments(1)] == ["Later evidence", refused["body"]]
    assert ctx.store.github_outbox() == []


def test_evidence_publishes_while_label_send_is_in_flight(outbox_context, monkeypatch):
    ctx = outbox_context
    ctx.clock[0] = ctx.reset
    send = ctx.remote.add_labels

    def labels(number, values):
        from foragent import orch_advice
        assert orch_advice.publish(ctx.tracker, ctx, number, "  Exact advice\n") is None
        assert [comment.body for comment in ctx.remote.list_comments(number)] == ["  Exact advice\n"]
        return send(number, values)

    monkeypatch.setattr(ctx.remote, "add_labels", labels)
    ctx.tracker.add_labels(1, ["ready"])
    assert ctx.store.github_outbox() == []


@pytest.mark.parametrize("read", ["current_issue", "sub_issues"])
def test_evidence_does_not_mask_current_remote_issue(outbox_context, read):
    ctx = outbox_context
    ctx.store.queue_github_write(1, "comment", body="Advice", evidence=True)
    ctx.remote.add_issue(2, "Parent")
    ctx.remote.add_sub_issue(2, 1)
    ctx.remote.add_labels(1, ["human"])
    if read == "current_issue":
        issue = ctx.tracker.current_issue(1)
    else:
        issue, = ctx.tracker.sub_issues(2)
    assert "human" in issue.labels
    assert issue == ctx.remote.get_issue(1)
    assert len(ctx.store.github_outbox()) == 1


@pytest.mark.parametrize("evidence", [True, False])
def test_only_evidence_outbox_allows_new_run(scheduler_context, evidence):
    from foragent import scheduler, scheduler_start
    from test_scheduler import approve

    ctx = scheduler_context
    approve(ctx)
    scheduler.tick(ctx)
    ctx.store.queue_github_write(1, "comment", body="Pending", **({"evidence": True} if evidence else {}))
    launched = []
    ctx.launcher = lambda number: launched.append(number) or 7001
    report = scheduler.TickReport()
    result = scheduler_start._start_ready(ctx, {"main": [1]}, lambda issue: "main", "origin/main",
                                          report, ("normal", 0.0))
    assert result is evidence
    assert launched == ([1] if evidence else [])
    assert len(ctx.store.github_outbox()) == 1
    assert ("running" in ctx.tracker.get_issue(1).labels) is evidence


def test_older_evidence_keeps_foreground_publication_in_order(outbox_context):
    from foragent import orch_advice

    ctx = outbox_context
    orch_advice.publish(ctx.tracker, ctx, 1, "First")
    ctx.clock[0] = ctx.reset
    orch_advice.publish(ctx.tracker, ctx, 1, "Second")
    assert ctx.remote.list_comments(1) == []
    assert [entry["body"] for entry in ctx.store.github_outbox()] == ["First", "Second"]
    ctx.tracker.flush_outbox()
    ctx.tracker.flush_outbox()
    assert [comment.body for comment in ctx.remote.list_comments(1)] == ["First", "Second"]
    assert ctx.store.github_outbox() == []


@pytest.mark.parametrize("action", ["comment", "add_labels", "remove_labels", "update_issue", "close_issue"])
@pytest.mark.parametrize("same_issue", [False, True])
@pytest.mark.parametrize("queue_writes", [False, True])
def test_evidence_never_creates_workflow_debt(outbox_context, monkeypatch, action, same_issue, queue_writes):
    from foragent import orch_advice

    ctx = outbox_context
    ctx.remote.add_issue(2, "Other issue", labels=["running"])
    ctx.remote.add_labels(1, ["running"])
    ctx.tracker.get_issue(2)
    orch_advice.publish(ctx.tracker, ctx, 1, "Refused evidence")
    evidence, = ctx.store.github_outbox()
    ctx.clock[0] = ctx.reset
    post = ctx.remote.comment

    def comment(number, body):
        if body == evidence["body"]:
            raise TrackerError("HTTP 422 Body is too long")
        return post(number, body)

    monkeypatch.setattr(ctx.remote, "comment", comment)
    ctx.tracker.flush_outbox()
    ctx.tracker.queue_writes = queue_writes
    target = 1 if same_issue else 2
    operations = {
        "comment": lambda: ctx.tracker.comment(target, "Workflow transition"),
        "add_labels": lambda: ctx.tracker.add_labels(target, ["ready"]),
        "remove_labels": lambda: ctx.tracker.remove_labels(target, ["running"]),
        "update_issue": lambda: ctx.tracker.update_issue(target, title="Changed", body="New body"),
        "close_issue": lambda: ctx.tracker.close_issue(target),
    }
    operations[action]()
    actual = ctx.remote.get_issue(target)
    effects = {
        "comment": [comment.body for comment in ctx.remote.list_comments(target)] == ["Workflow transition"],
        "add_labels": "ready" in actual.labels,
        "remove_labels": "running" not in actual.labels,
        "update_issue": (actual.title, actual.body) == ("Changed", "New body"),
        "close_issue": actual.state == "closed",
    }
    assert effects[action], "Evidence must not defer an otherwise writable workflow operation"
    assert ctx.store.github_outbox() == [evidence]
    assert ctx.store.github_blocking_outbox() == []


@pytest.mark.parametrize("error", ["HTTP 403 Issue is locked", "HTTP 422 Body is too long"])
def test_tick_starts_after_post_flush_transition_despite_other_issue_evidence(
    scheduler_context, monkeypatch, error,
):
    from foragent import orch_advice, scheduler
    from test_scheduler import approve

    ctx = scheduler_context
    approve(ctx)
    scheduler.tick(ctx)
    remote = ctx.tracker._tracker
    remote.add_issue(2, "Evidence owner")
    post = remote.comment
    events, launched = [], []

    def comment(number, body):
        if number == 2:
            events.append("evidence")
            raise TrackerError(error)
        events.append("posted")
        return post(number, body)

    monkeypatch.setattr(remote, "comment", comment)
    assert orch_advice.publish(ctx.tracker, ctx, 2, "Refused evidence")
    evidence, = ctx.store.github_outbox()
    events.clear()
    waits = scheduler.orch_waits.tick

    def transition_after_flush(context, report, **kwargs):
        result = waits(context, report, **kwargs)
        events.append("transition")
        context.tracker.comment(1, "Workflow transition")
        return result

    monkeypatch.setattr(scheduler.orch_waits, "tick", transition_after_flush)

    def launch(number):
        events.append("launch")
        launched.append(number)
        return 7001

    ctx.launcher, ctx.start_runs = launch, True
    scheduler.tick(ctx)
    assert launched == [1]
    assert events.index("evidence") < events.index("transition") < events.index("launch")
    assert [comment.body for comment in remote.list_comments(1)].count("Workflow transition") == 1
    assert remote.get_issue(1).labels == {"approved", "running"}
    assert ctx.store.get_run(1).status == "running"
    assert ctx.store.github_outbox() == [evidence]
    assert ctx.store.github_blocking_outbox() == []


@pytest.mark.parametrize("same_issue", [False, True])
@pytest.mark.parametrize("queue_writes", [False, True])
def test_evidence_does_not_hide_workflow_comment_errors(outbox_context, monkeypatch, same_issue, queue_writes):
    ctx = outbox_context
    evidence = ctx.store.queue_github_write(1, "comment", body="Evidence", evidence=True)
    ctx.remote.add_issue(2, "Other issue")
    ctx.clock[0] = ctx.reset
    ctx.tracker.queue_writes = queue_writes

    def refuse(number, body):
        raise TrackerError("HTTP 422 Workflow comment refused")

    monkeypatch.setattr(ctx.remote, "comment", refuse)
    with pytest.raises(TrackerError, match="Workflow comment refused"):
        ctx.tracker.comment(1 if same_issue else 2, "Workflow transition")
    assert ctx.store.github_outbox() == [evidence]
    assert ctx.store.github_blocking_outbox() == []
