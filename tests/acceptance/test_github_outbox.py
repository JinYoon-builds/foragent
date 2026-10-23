"""Acceptance test for github-outbox. Locked by Foragent; do not edit."""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
import json
import subprocess
from urllib.parse import unquote, urlsplit

import pytest

from test_local_truth import local_rows, record_gh
from test_local_view import github_reply
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home


def outbox_rows(store):
    """Public persisted FIFO: issue/action/queued_at, plus labels or body.

    Actions use Tracker method names. A close's optional comment uses body too.
    Additional delivery metadata is allowed; no on-disk layout is prescribed.
    """
    reader = getattr(store, "github_outbox", None)
    assert callable(reader), "StateStore needs a public github_outbox() pending-write API"
    rows = [asdict(row) if is_dataclass(row) else dict(row) for row in reader()]
    for row in rows:
        assert {"issue", "action", "queued_at"} <= row.keys(), f"Incomplete outbox entry: {row}"
        assert isinstance(row["issue"], int)
        assert datetime.fromisoformat(row["queued_at"]).utcoffset() is not None
        assert row["action"] in {"add_labels", "remove_labels", "comment", "close_issue"}
        if row["action"] in {"add_labels", "remove_labels"}:
            assert isinstance(row.get("labels"), (list, tuple)) and row["labels"]
        if row["action"] == "comment":
            assert isinstance(row.get("body"), str) and row["body"]
    return rows


def github_outbox_case(git_history, tmp_path, monkeypatch, *, action="add_labels", status=403):
    """Real GhTracker writes/errors; GitHub's state lives only in the fake.

    Unlike github_case's read error, this transport rejects writes until reset.
    Successful reads remain available, and updated_at changes only on a mutation.
    """
    from foragent.scheduler import tick
    from foragent.tracker import GhTracker

    record_gh(monkeypatch)  # Any gh call outside the injected transport is a test failure.
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, outcome="spec-question")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked"])
    case.tracker.record_label(1, "approved", "human")
    tick(tick_context(case, start_runs=False))
    case.reset = case.clock[0] + timedelta(minutes=10)
    initial_updated_at = case.clock[0].isoformat()
    case.remote_updated_at = {1: initial_updated_at}
    case.gh_calls, case.write_attempts, case.delivered, case.errors = [], [], [], []
    case.limited = False

    for name in ("record_label", "remove_labels", "close_issue"):
        original = getattr(case.tracker, name)

        def mutate(number, *args, _original=original, **kwargs):
            result = _original(number, *args, **kwargs)
            case.remote_updated_at[number] = case.clock[0].isoformat()
            return result

        monkeypatch.setattr(case.tracker, name, mutate)

    def gh_run(argv, input=None):
        case.gh_calls.append(["gh", *argv])
        if argv[:2] == ["issue", "close"]:
            number, method = int(argv[2]), "close_issue"
            payload = argv[argv.index("--comment") + 1] if "--comment" in argv else None
        elif argv[0] == "api" and "-X" in argv and argv[argv.index("-X") + 1] != "GET":
            verb = argv[argv.index("-X") + 1]
            endpoint = next(arg for arg in argv if arg.startswith("repos/owner/repo/"))
            parts = urlsplit(endpoint).path.removeprefix("repos/owner/repo/").split("/")
            assert parts[0] == "issues", f"Unexpected write endpoint: {argv}"
            number = int(parts[1])
            if parts[2:] == ["labels"] and verb == "POST":
                method = "add_labels"
                payload = tuple(arg.removeprefix("labels[]=") for arg in argv if arg.startswith("labels[]="))
            elif parts[2:3] == ["labels"] and verb == "DELETE":
                method, payload = "remove_labels", (unquote(parts[3]),)
            else:
                assert parts[2:] == ["comments"] and verb == "POST", argv
                method, payload = "comment", json.loads(input)["body"]
        else:
            if argv[:2] == ["api", "rate_limit"]:
                window = {"limit": 5000, "remaining": 0, "used": 5000,
                          "reset": int(case.reset.timestamp())}
                return json.dumps({"resources": {"core": window, "graphql": window}, "rate": window})
            if argv[:2] == ["api", "graphql"]:
                number = next(int(arg.removeprefix("number=")) for arg in argv if arg.startswith("number="))
                return json.dumps({"data": {"repository": {"issue": {
                    "lastEditedAt": case.tracker.body_last_edited_at(number),
                }}}})
            data = json.loads(github_reply(case, argv))
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and "labels" in item:
                    item["updated_at"] = case.remote_updated_at.get(item["number"], initial_updated_at)
            return json.dumps(data)

        case.write_attempts.append((number, method, payload))
        if method == action:
            case.limited = True
        if case.limited and case.clock[0] < case.reset:
            message = ("gh: API rate limit exceeded for offline-user (HTTP 403)" if status == 403
                       else "gh: HTTP 422 Validation Failed")
            case.errors.append(message)
            headers = (f"HTTP/2 {status}\r\nX-RateLimit-Reset: {int(case.reset.timestamp())}\r\n\r\n"
                       if status == 403 else "")
            # GhTracker must do the actual CalledProcessError -> TrackerError conversion.
            raise subprocess.CalledProcessError(1, ["gh", *argv], output=headers, stderr=message)
        result = getattr(case.tracker, method)(number, payload)
        case.delivered.append((number, method, payload))
        return json.dumps({"id": result}) if method == "comment" else "[]"

    case.github = GhTracker("owner/repo", run=gh_run)
    case.ctx.tracker = case.github
    return case


def persisted_store(case):
    from foragent.state import StateStore

    return StateStore(case.store.root, now=lambda: case.clock[0])


def outbox_tick(case):
    """Use a fresh store and tick context, as a later gate process would."""
    from foragent.scheduler import tick

    ctx = tick_context(case, start_runs=False)
    ctx.tracker, ctx.store = case.github, persisted_store(case)
    return tick(ctx)


def assert_retry_pending(case, first_action, summary):
    rows = outbox_rows(persisted_store(case))
    actions = ["add_labels", "remove_labels", "comment"]
    assert [row["action"] for row in rows] == actions[actions.index(first_action):]
    for row in rows:
        assert row["issue"] == 1
        assert datetime.fromisoformat(row["queued_at"]) == case.clock[0]
        if row["action"] == "add_labels":
            assert list(row["labels"]) == ["ready"]
        elif row["action"] == "remove_labels":
            assert set(row["labels"]) == {"blocked", "needs-human"}
        else:
            assert row["body"] == "<!-- foragent:orch -->\n" + summary
    return rows


@pytest.mark.parametrize("action", ["add_labels", "remove_labels", "comment"])
def test_github_outbox_retry_keeps_local_record_and_queues_rate_limited_writes(
    git_history, tmp_path, monkeypatch, action,
):
    from foragent import orch

    case = github_outbox_case(git_history, tmp_path, monkeypatch, action=action)
    summary = orch.retry(case.ctx, 1)
    assert case.errors, "The real GhTracker write must encounter the rate limit"
    assert "queued implementation retry" in summary
    store = persisted_store(case)
    row = local_rows(store)[1]
    assert row["state"] == "open" and set(row["labels"]) == {"approved", "ready"}
    assert_retry_pending(case, action, summary)
    assert store.load_flag(1) is None
    assert not any("rate_limit" in argv for argv in case.gh_calls), "The 403 already carries its reset time"
    assert case.implementer.calls == case.reviewer.calls == []


def test_github_outbox_tick_waits_until_reset_then_flushes_in_order(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch

    case = github_outbox_case(git_history, tmp_path, monkeypatch)
    summary = orch.retry(case.ctx, 1)
    pending = assert_retry_pending(case, "add_labels", summary)
    attempts = list(case.write_attempts)
    case.clock[0] = case.reset - timedelta(seconds=1)
    outbox_tick(case)
    assert case.write_attempts == attempts, "A tick before reset attempted a GitHub write"
    assert outbox_rows(persisted_store(case)) == pending
    assert set(local_rows(persisted_store(case))[1]["labels"]) == {"approved", "ready"}

    case.clock[0] = case.reset
    outbox_tick(case)
    expected = []
    for row in pending:
        if row["action"] == "remove_labels":
            expected.extend((row["issue"], row["action"], (label,)) for label in row["labels"])
        else:
            payload = tuple(row["labels"]) if row["action"] == "add_labels" else row["body"]
            expected.append((row["issue"], row["action"], payload))
    assert case.delivered == expected, "The reset tick did not deliver the pending writes in FIFO order"
    assert case.write_attempts[len(attempts):] == expected
    assert outbox_rows(persisted_store(case)) == []
    row = local_rows(persisted_store(case))[1]
    remote = case.tracker.get_issue(1)
    assert row["state"] == remote.state == "open"
    assert set(row["labels"]) == set(remote.labels) == {"approved", "ready"}
    assert [comment.body for comment in case.tracker.list_comments(1)] == [pending[-1]["body"]]
    assert persisted_store(case).load_flag(1) is None
    case.clock[0] += timedelta(minutes=5)
    outbox_tick(case)
    assert case.delivered == expected, "An already flushed write was sent twice"


def test_github_outbox_flush_drops_changed_issue_and_refreshes_from_github(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch

    case = github_outbox_case(git_history, tmp_path, monkeypatch)
    summary = orch.retry(case.ctx, 1)
    assert_retry_pending(case, "add_labels", summary)
    case.clock[0] += timedelta(minutes=1)
    case.tracker.record_label(1, "needs-human", "human")
    human_issue = case.tracker.get_issue(1)
    attempts = list(case.write_attempts)
    case.clock[0] = case.reset
    outbox_tick(case)
    assert case.write_attempts == attempts, "A conflicting queued write overwrote the person's change"
    assert outbox_rows(persisted_store(case)) == []
    events = orch.log(case.ctx)
    assert any(event["kind"] == "outbox-dropped" and event["issue"] == 1 for event in events), (
        "Dropping conflicting writes must leave an outbox-dropped event naming issue #1")
    row = local_rows(persisted_store(case))[1]
    assert set(row["labels"]) == set(human_issue.labels)
    assert row["state"] == human_issue.state
    assert case.tracker.get_issue(1) == human_issue
    assert case.tracker.list_comments(1) == [], "A dropped retry comment was still posted"
    case.clock[0] += timedelta(minutes=5)
    outbox_tick(case)
    assert case.write_attempts == attempts
    assert case.tracker.get_issue(1) == human_issue


def test_github_outbox_gate_preserves_and_flushes_rate_limited_close(
    git_history, tmp_path, monkeypatch,
):
    case = github_outbox_case(git_history, tmp_path, monkeypatch, action="close_issue")
    case.tracker.add_issue(2, "Batch parent")
    case.tracker.set_parent(1, 2)
    pr = case.tracker.create_pr(head="foragent/issue-1", base="foragent/batch-2",
                                title="Merged child", body="Closes #1")
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    # The parent PR already exists, so reconciliation needs no new PR creation.
    case.tracker.create_pr(head="foragent/batch-2", base="main", title="Batch", body="Closes #2")
    case.clock[0] += timedelta(minutes=5)
    outbox_tick(case)
    assert case.errors, "The gate must hit a real rate-limited close write"
    assert local_rows(persisted_store(case))[1]["state"] == "closed"
    assert case.tracker.get_issue(1).state == "open"
    pending = outbox_rows(persisted_store(case))
    closes = [row for row in pending if row["issue"] == 1 and row["action"] == "close_issue"]
    assert len(closes) == 1
    assert closes[0]["body"] == f"Merged into foragent/batch-2 (PR #{pr})"
    attempts = list(case.write_attempts)
    case.clock[0] = case.reset - timedelta(seconds=1)
    outbox_tick(case)
    assert case.write_attempts == attempts
    case.clock[0] = case.reset
    outbox_tick(case)
    assert case.tracker.get_issue(1).state == local_rows(persisted_store(case))[1]["state"] == "closed"
    assert outbox_rows(persisted_store(case)) == []
    assert [write for write in case.delivered if write[1] == "close_issue"] == [
        (1, "close_issue", closes[0]["body"])]
    assert sum(comment.body == closes[0]["body"] for comment in case.tracker.list_comments(1)) == 1
    assert persisted_store(case).load_flag(1) is None


def test_github_outbox_non_rate_limit_write_error_is_not_queued(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch
    from foragent.tracker import TrackerError

    case = github_outbox_case(git_history, tmp_path, monkeypatch, status=422)
    with pytest.raises(TrackerError, match="HTTP 422 Validation Failed"):
        orch.retry(case.ctx, 1)
    assert case.errors == ["gh: HTTP 422 Validation Failed"]
    assert outbox_rows(persisted_store(case)) == [], "Validation errors must never enter the retry outbox"
    assert not any("rate_limit" in argv for argv in case.gh_calls)
    assert case.delivered == []
