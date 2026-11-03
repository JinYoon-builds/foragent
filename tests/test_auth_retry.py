"""Boundary tests for bounded Git/GitHub authentication retries (#1208)."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from foragent import auth_retry
from foragent.git import GitError, GitRepo
from foragent.tracker import GhTracker, TrackerError


@pytest.mark.parametrize("text", [
    "git@github.com: Permission denied (publickey).",
    "fatal: Could not read from remote repository.",
    "remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/o/r/'",
    "gh: Bad credentials (HTTP 401)",
    "HTTP/2 401 Unauthorized",
])
def test_authentication_failures_are_recognised(text):
    assert auth_retry.is_auth_failure(text)
    assert GitError(text).is_auth_failure
    assert TrackerError(text).is_auth_failure


@pytest.mark.parametrize("text", [
    "fatal: couldn't find remote ref missing-branch",
    "gh: Resource not accessible by integration (HTTP 403)",
    "gh: Not Found (HTTP 404)",
    "HTTP 4010 weird",
])
def test_other_failures_are_not_authentication(text):
    assert not auth_retry.is_auth_failure(text)
    assert not GitError(text).is_auth_failure
    assert not TrackerError(text).is_auth_failure


def test_rate_limit_with_bad_credentials_wording_stays_a_rate_limit():
    error = TrackerError("API rate limit exceeded; Bad credentials (HTTP 401)")
    assert error.is_rate_limit and not error.is_auth_failure


def test_describe_is_one_line_naming_the_failure():
    detail = auth_retry.describe(GitError("warning: x\ngit@github.com: Permission denied (publickey).\nfatal: y\n"))
    assert detail == ("authentication failed after 3 attempts: "
                      "git@github.com: Permission denied (publickey).")


def failing(message, failures):
    calls = []

    def run(args, **kwargs):
        calls.append(list(args))
        if len(calls) <= failures:
            raise subprocess.CalledProcessError(128, ["git", *args], stderr=message)
        return "ok"
    return run, calls


def test_git_remote_command_retries_with_growing_waits_then_succeeds(monkeypatch):
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    run, calls = failing("Permission denied (publickey).", 2)
    assert GitRepo(Path("."), run).fetch("origin", "main") is None
    assert len(calls) == 3
    assert waits == list(auth_retry.WAITS) and waits[1] > waits[0]


def test_git_persistent_authentication_failure_stops_after_the_budget(monkeypatch):
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    run, calls = failing("fatal: Could not read from remote repository.", 99)
    with pytest.raises(auth_retry.AuthExhausted) as raised:
        GitRepo(Path("."), run).push("foragent/issue-1")
    assert raised.value.is_auth_failure
    assert len(calls) == auth_retry.ATTEMPTS and len(waits) == auth_retry.ATTEMPTS - 1


def test_git_child_repository_shares_one_budget(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    run, calls = failing("Permission denied (publickey).", 99)
    with pytest.raises(auth_retry.AuthExhausted):
        GitRepo(Path("."), run).at(Path("/tmp/wt")).push("foragent/issue-1")
    assert len(calls) == auth_retry.ATTEMPTS
    assert calls[0][:2] == ["-C", "/tmp/wt"]


@pytest.mark.parametrize("args", [["rev-parse", "HEAD"], ["-C", "/tmp/wt", "status"]])
def test_local_git_commands_are_not_retried(monkeypatch, args):
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("local Git must not wait"))
    run, calls = failing("Permission denied (publickey).", 99)
    with pytest.raises(GitError):
        GitRepo(Path("."), run)._call(args)
    assert len(calls) == 1


def test_git_non_authentication_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("unrelated errors must not wait"))
    run, calls = failing("fatal: couldn't find remote ref missing-branch", 99)
    with pytest.raises(GitError):
        GitRepo(Path("."), run).fetch("origin", "missing-branch")
    assert len(calls) == 1


def gh_failing(message, failures, returncode=1):
    calls = []

    def run(args, input=None):
        calls.append(list(args))
        if len(calls) <= failures:
            raise subprocess.CalledProcessError(returncode, ["gh", *args], stderr=message)
        return "[]"
    return run, calls


def test_gh_authentication_failure_retries_then_succeeds_with_injected_sleep():
    waits = []
    run, calls = gh_failing("gh: Bad credentials (HTTP 401)", 1)
    assert GhTracker("o/r", run=run, sleep=waits.append).find_pr("foragent/issue-1") is None
    assert len(calls) == 2 and waits == [auth_retry.WAITS[0]]


def test_gh_default_sleep_is_looked_up_at_call_time(monkeypatch):
    tracker = GhTracker("o/r", run=gh_failing("gh: Bad credentials (HTTP 401)", 99)[0])
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    with pytest.raises(auth_retry.AuthExhausted) as raised:
        tracker.find_pr("foragent/issue-1")
    assert raised.value.is_auth_failure and waits == list(auth_retry.WAITS)


@pytest.mark.parametrize("message", [
    "gh: Resource not accessible by integration (HTTP 403)",
    "gh: API rate limit exceeded (HTTP 403)",
])
def test_gh_other_failures_are_not_retried(message):
    run, calls = gh_failing(message, 99)
    with pytest.raises(TrackerError):
        GhTracker("o/r", run=run, sleep=lambda seconds: pytest.fail("must not wait")).find_pr("x")
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "To get started with GitHub CLI, please run:  gh auth login",
    "You are not logged into any GitHub hosts. To log in, run: gh auth login",
    "sign_and_send_pubkey: signing failed for ED25519 \"key\" from agent: agent refused operation\n"
    "git@github.com: Permission denied (publickey).",
    "fatal: unable to access 'https://github.com/o/r/': The requested URL returned error: 401",
    "gh: To use GitHub CLI in automation, set the GH_TOKEN environment variable.",
    "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable. Example:\n"
    "  env:\n    GH_TOKEN: ${{ github.token }}",
])
def test_missing_or_refused_credentials_are_recognised(text):
    assert auth_retry.is_auth_failure(text)


@pytest.mark.parametrize("cause", [
    "ssh: Could not resolve hostname github.com: nodename nor servname provided, or not known",
    "ssh: connect to host github.com port 22: Connection refused",
    "ssh: connect to host github.com port 22: Operation timed out",
    "kex_exchange_identification: Connection closed by remote host",
    "ERROR: Repository not found.",
])
def test_generic_trailer_after_a_network_or_repository_diagnostic_is_not_authentication(cause):
    assert not auth_retry.is_auth_failure(f"{cause}\nfatal: Could not read from remote repository.\n")


COMPOSITES = {
    "issue-close": lambda tracker: tracker.close_issue(1, "Merged into batch."),
    "pr-close": lambda tracker: tracker.close_pr(2, "Closed by retry"),
    "pr-merge": lambda tracker: tracker.merge_pr(2, expected_head_sha="a" * 40, subject="T (#1)"),
}


@pytest.mark.parametrize("command", COMPOSITES)
def test_a_command_that_sends_several_writes_is_attempted_once(command):
    """#1208 scope A: only one-step idempotent calls retry; a multi-write command fails as before."""
    run, calls = gh_failing("gh: Bad credentials (HTTP 401)", 99)
    with pytest.raises(auth_retry.AuthExhausted) as raised:
        COMPOSITES[command](GhTracker("o/r", run=run, sleep=lambda seconds: pytest.fail("must not wait")))
    assert raised.value.is_auth_failure and raised.value.attempts == 1
    assert "after 1 attempt:" in auth_retry.describe(raised.value)
    assert [call[:2] for call in calls if call[:2] != ["pr", "view"]] == [
        {"issue-close": ["issue", "close"], "pr-close": ["pr", "close"], "pr-merge": ["pr", "merge"]}[command]]


def test_a_single_write_still_retries():
    run, calls = gh_failing("gh: Bad credentials (HTTP 401)", 1)
    GhTracker("o/r", run=run, sleep=lambda seconds: None).close_issue(1)  # one request: issue close, no comment
    assert len(calls) == 2


def test_stdout_never_decides_authentication():
    error = TrackerError("exit status 1", stdout='[{"name":"Authentication failed","bucket":"fail"}]', stderr="")
    assert not error.is_auth_failure
    assert not GitError("Command ['git', 'push', 'Authentication failed'] returned 1", stderr="").is_auth_failure


def test_failed_ci_check_named_like_an_authentication_failure_is_not_retried():
    calls = []

    def run(args, input=None):
        calls.append(args)
        raise subprocess.CalledProcessError(
            1, ["gh", *args], output='[{"name":"Authentication failed","bucket":"fail","workflow":"","link":""}]',
            stderr="")
    assert GhTracker("o/r", run=run, sleep=lambda seconds: pytest.fail("must not wait")).pr_checks(2) == "fail"
    assert len(calls) == 1


def test_retitle_and_pr_refresh_propagate_authentication_failures(tmp_path):
    from types import SimpleNamespace
    from foragent import run_publish

    def refuse(*args, **kwargs):
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))  # what the transport raises once the budget is spent
    notes = []
    ctx = SimpleNamespace(tracker=SimpleNamespace(update_pr=refuse, list_prs=refuse))
    with pytest.raises(auth_retry.AuthExhausted):
        run_publish.retitle_for_merge(ctx, 2, title="New", current_title="Old", base="main",
                                      body=lambda: "", note=notes.append)
    attempt = SimpleNamespace(ctx=ctx, target_branch="main", log=tmp_path / "log", branch="b", pr_title=None)
    with pytest.raises(auth_retry.AuthExhausted):
        run_publish._update_open_pr(attempt, 2, "New", "")
    assert notes == []


@pytest.fixture
def run_case(git_history, tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="none",
                     language="en", skill_libraries=[], test_command="true", max_retries=0)
    case.tracker.set_pr_files(2, ["feature.txt"])
    case.tracker.set_pr_checks(2, "pass")
    case.ctx.poll_hook = lambda execute, progress: execute()
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return case


def test_merge_authentication_failure_ends_the_run_once_without_rejoining_the_queue(run_case, monkeypatch):
    from foragent.runner import run_issue

    case, merges = run_case, []

    def refuse(pr, **kwargs):
        merges.append(pr)
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))

    def refuse_with_ready(pr, **kwargs):
        case.tracker.add_labels(1, ["ready"])  # a ready label left behind (the runner's limit wait adds one too)
        return refuse(pr, **kwargs)
    monkeypatch.setattr(case.tracker, "merge_pr", refuse_with_ready)
    assert run_issue(1, case.ctx) == "auth-failed"
    assert merges == [2], "a refused merge rejoined the merge queue"
    from foragent import run_merge_turn

    turns = [row["result"] for row in run_merge_turn._rows(case.store) if row["event"] == "turn" and row["n"] == 1]
    assert turns[-1:] == ["failed"], "the refused merge is still recorded as merged"
    run = case.store.get_run(1)
    assert run.outcome == "auth-failed" and len(run.detail.splitlines()) == 1 and "401" in run.detail
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human", "running", "ready"}
    assert case.store.load_flag(1) is None
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events if event.get("issue") == 1].count("auth-failed") == 1


@pytest.mark.parametrize("message,raised", [("gh: Bad credentials (HTTP 401)", True),
                                            ("gh: Something else (HTTP 500)", False)])
def test_board_sync_lets_exhausted_authentication_end_the_run(monkeypatch, message, raised):
    from types import SimpleNamespace
    from foragent import board

    def refuse(issue, status):
        error = TrackerError(message)
        raise auth_retry.AuthExhausted(error) if error.is_auth_failure else error
    monkeypatch.setattr(board, "set_status", refuse)
    monkeypatch.setattr(board, "_order_parent", lambda ctx, n: False)
    monkeypatch.setattr(board, "_waits_on_person", lambda ctx, issue: False)
    ctx = SimpleNamespace(config=SimpleNamespace(board="PVT_x", board_map={}), tracker=object(), repo="o/r")
    if raised:
        with pytest.raises(auth_retry.AuthExhausted):
            board.sync_status(ctx, 1, "Running")
    else:
        board.sync_status(ctx, 1, "Running")  # a board hiccup stays a warning


def auth_events(case):
    path = case.store.root / "orch-events.jsonl"
    return [event for event in map(json.loads, path.read_text().splitlines())
            if event.get("issue") == 1 and event["kind"] == "auth-failed"]


def test_exhausted_authentication_after_the_merge_keeps_the_run_merged_with_its_reason(run_case, monkeypatch):
    from foragent.runner import run_issue

    case = run_case

    def refuse(n, comment=None):
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))
    monkeypatch.setattr(case.tracker, "close_issue", refuse)
    assert run_issue(1, case.ctx) == "merged"
    assert case.tracker.merged_prs == [2]
    run = case.store.get_run(1)
    assert run.outcome == "merged" and "401" in run.detail and len(run.detail.splitlines()) == 1
    assert len(auth_events(case)) == 1
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert case.store.load_flag(1) is None


def test_review_only_cleanup_refused_by_the_credential_is_recorded_not_raised(run_case, monkeypatch):
    from foragent import runner

    from datetime import timedelta

    case = run_case
    if (previous := case.store.get_run(1)) is not None and previous.status == "running":
        case.store.finish_run(1, "pr-open")
    case.clock[0] += timedelta(minutes=1)
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(runner, "_review_only", lambda n, ctx, worktree, head: "pr-open")

    def refuse(n):
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))
    monkeypatch.setattr(case.ctx.tracker, "current_issue", refuse, raising=False)
    assert runner.review_only(1, case.ctx) == "auth-failed"
    run = case.store.get_run(1)
    assert run.status == "finished" and run.outcome == "auth-failed" and "401" in run.detail
    assert len(auth_events(case)) == 1


def test_batch_finalization_refused_by_the_credential_keeps_merged_and_its_reason(git_history, tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))
    from test_orch_review_handoff import make_case
    from foragent import run_publish
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="none", review="batch",
                     language="en", skill_libraries=[], test_command="true", max_retries=0)
    case.tracker.add_issue(9, "Parent", "parent", labels=[])
    case.tracker.add_sub_issue(9, 1)
    case.ctx.poll_hook = lambda execute, progress: execute()
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    finalized = []

    def refuse(parent, ctx):
        finalized.append(parent)
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))
    monkeypatch.setattr(run_publish, "_batch_pr", refuse)
    assert run_issue(1, case.ctx) == "merged"
    assert finalized == [9] and len(case.tracker.merged_prs) == 1
    run = case.store.get_run(1)
    assert run.outcome == "merged" and "401" in run.detail
    assert len(auth_events(case)) == 1


def test_review_only_refused_before_it_starts_records_the_reason(run_case, monkeypatch):
    from datetime import timedelta
    from foragent import runner

    case = run_case
    if (previous := case.store.get_run(1)) is not None and previous.status == "running":
        case.store.finish_run(1, "pr-open")
    case.clock[0] += timedelta(minutes=1)

    def refuse(n):
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))
    monkeypatch.setattr(case.ctx.tracker, "get_issue", refuse, raising=False)
    assert runner.review_only(1, case.ctx) == "auth-failed"
    run = case.store.get_run(1)
    assert run.status == "finished" and run.outcome == "auth-failed" and "401" in run.detail
    assert len(auth_events(case)) == 1


def test_reinstall_fetch_refused_after_the_merge_keeps_merged_with_its_reason(run_case, monkeypatch):
    from foragent import orch_reinstall, runner

    case = run_case

    def refuse(ctx, base):
        raise auth_retry.AuthExhausted(GitError("git@github.com: Permission denied (publickey)."))
    monkeypatch.setattr(orch_reinstall, "_reinstall", refuse)
    monkeypatch.setattr(runner, "after_merge", orch_reinstall.after_merge)
    assert runner.run_issue(1, case.ctx) == "merged"
    run = case.store.get_run(1)
    assert run.outcome == "merged" and "publickey" in run.detail
    assert len(auth_events(case)) == 1


def test_a_missing_token_in_automation_retries_and_is_no_outage_wait():
    """#1208: gh's automation hint has no HTTP status; it is a credential failure, never `transport`."""
    message = "gh: To use GitHub CLI in automation, set the GH_TOKEN environment variable."
    run, calls = gh_failing(message, 99)
    with pytest.raises(auth_retry.AuthExhausted) as raised:
        GhTracker("o/r", run=run, sleep=lambda seconds: None).find_pr("foragent/issue-1")
    assert len(calls) == auth_retry.ATTEMPTS and not raised.value.cause.waits


def outbox_case(tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, RecordingTracker

    clock = [datetime(2026, 11, 3, 9, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(7, "Title", "Body", labels=["approved", "ready"])
    context = SimpleNamespace(store=store, config=SimpleNamespace(lanes={}), now=lambda: clock[0])
    writer = RecordingTracker(remote, context, queue_writes=True)
    writer.list_issues()
    return SimpleNamespace(clock=clock, store=store, remote=remote, writer=writer)


def refusing(calls):
    def refuse(*args, **kwargs):
        calls.append(args)
        raise auth_retry.AuthExhausted(TrackerError("gh: Bad credentials (HTTP 401)"))
    return refuse


def test_a_write_refused_for_the_whole_budget_is_not_left_pending(tmp_path, monkeypatch):
    """#1208: a pending intent would hold later writes and be resent with a fresh budget every tick."""
    case, calls = outbox_case(tmp_path), []
    monkeypatch.setattr(case.remote, "add_labels", refusing(calls))
    with pytest.raises(auth_retry.AuthExhausted):
        case.writer.add_labels(7, ["running"])
    assert case.store.github_outbox() == []
    case.writer.flush_outbox()
    assert len(calls) == 1, "the refused write was resent"
    case.writer.remove_labels(7, ["ready"])  # cleanup is not queued behind it
    assert "ready" not in case.remote.get_issue(7).labels


def test_a_queued_write_refused_while_flushing_is_abandoned_not_resent(tmp_path, monkeypatch):
    from datetime import timedelta

    case, calls = outbox_case(tmp_path), []
    case.store.pause_github(case.clock[0] + timedelta(hours=1))
    case.writer.remove_labels(7, ["ready"])  # queued behind the GitHub wait
    assert len(case.store.github_outbox()) == 1
    case.clock[0] += timedelta(hours=2)
    monkeypatch.setattr(case.remote, "remove_labels", refusing(calls))
    with pytest.raises(auth_retry.AuthExhausted):
        case.writer.flush_outbox()
    case.writer.flush_outbox()
    assert len(calls) == 1 and case.store.github_outbox() == []


def test_stdout_data_mentioning_a_rate_limit_does_not_hide_a_401():
    """#1208: earlier pages of a paginated read are data; gh's stderr decides."""
    error = TrackerError("gh: Bad credentials (HTTP 401)", stderr="gh: Bad credentials (HTTP 401)",
                         stdout='[{"title": "API rate limit exceeded in the nightly job"}]')
    assert error.is_auth_failure
    run, calls = [], None
    def page(args, input=None):
        run.append(args)
        raise subprocess.CalledProcessError(1, ["gh", *args], output='[{"title": "API rate limit exceeded"}]',
                                            stderr="gh: Bad credentials (HTTP 401)")
    with pytest.raises(auth_retry.AuthExhausted):
        GhTracker("o/r", run=page, sleep=lambda seconds: None).find_pr("foragent/issue-1")
    assert len(run) == auth_retry.ATTEMPTS
