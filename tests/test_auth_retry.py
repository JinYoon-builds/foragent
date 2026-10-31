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
    with pytest.raises(GitError) as raised:
        GitRepo(Path("."), run).push("foragent/issue-1")
    assert raised.value.is_auth_failure
    assert len(calls) == auth_retry.ATTEMPTS and len(waits) == auth_retry.ATTEMPTS - 1


def test_git_child_repository_shares_one_budget(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    run, calls = failing("Permission denied (publickey).", 99)
    with pytest.raises(GitError):
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
    with pytest.raises(TrackerError) as raised:
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


class FakeGitHub:
    """A gh runner over one issue/PR; ``partial`` names the effects the refused first attempt completed."""

    def __init__(self, partial=(), refusals=1):
        from datetime import UTC, datetime

        self.now = datetime.now(UTC).isoformat()
        self.partial, self.refusals = set(partial), refusals
        self.state, self.merges, self.branch = "OPEN", 0, True
        self.comments, self.calls = [], []

    def refuse(self, args):
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh: Bad credentials (HTTP 401)")

    def __call__(self, args, input=None):
        self.calls.append(list(args))
        if args[:2] in (["issue", "close"], ["pr", "close"], ["pr", "merge"]):
            composite = "--comment" in args or "--delete-branch" in args
            if composite and self.refusals:
                self.refusals -= 1
                if "comment" in self.partial:
                    self.comments.append(args[args.index("--comment") + 1])
                if "action" in self.partial:
                    self.act(args)
                self.refuse(args)
            assert not composite, f"a composite argv was sent again: {args}"
            self.act(args)
            return ""
        if args[:3] == ["api", "-X", "POST"] and args[3].endswith("/comments"):
            self.comments.append(json.loads(input)["body"])
            return json.dumps({"id": len(self.comments)})
        if args[:2] == ["api", "--paginate"] and "/comments" in args[2]:
            return json.dumps([{"id": i, "user": {"login": "fa"}, "body": body, "created_at": self.now}
                               for i, body in enumerate(self.comments)])
        if args[:3] == ["api", "-X", "DELETE"]:
            if not self.branch:
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh: Reference does not exist (HTTP 422)")
            self.branch = False
            return ""
        if args[:2] == ["pr", "view"]:
            return json.dumps({"state": self.state, "headRefName": "foragent/issue-1"})
        if args[:1] == ["api"] and args[1].endswith("/issues/1"):
            return json.dumps({"number": 1, "title": "T", "state": "closed" if self.state != "OPEN" else "open"})
        raise AssertionError(args)

    def act(self, args):
        if args[:2] == ["pr", "merge"]:
            self.merges += 1
            self.state = "MERGED"
        else:
            self.state = "CLOSED"
        if "--delete-branch" in args and "action" in self.partial and "branch" in self.partial:
            self.branch = False


@pytest.mark.parametrize("partial", [(), ("comment",), ("comment", "action")])
def test_refused_issue_close_with_comment_finishes_with_one_comment(partial):
    github = FakeGitHub(partial)
    GhTracker("o/r", run=github, sleep=lambda seconds: None).close_issue(1, "Merged into batch.")
    assert github.state == "CLOSED" and github.comments == ["Merged into batch."]


@pytest.mark.parametrize("partial", [(), ("comment",), ("comment", "action"), ("comment", "action", "branch")])
def test_refused_pr_close_finishes_once_and_deletes_the_branch(partial):
    github = FakeGitHub(partial)
    GhTracker("o/r", run=github, sleep=lambda seconds: None).close_pr(2, "Closed by retry")
    assert github.state == "CLOSED" and github.comments == ["Closed by retry"] and not github.branch


@pytest.mark.parametrize("partial", [(), ("action",), ("action", "branch")])
def test_refused_merge_waits_then_merges_exactly_once(partial):
    github, waits = FakeGitHub(partial), []
    GhTracker("o/r", run=github, sleep=waits.append).merge_pr(2, expected_head_sha="a" * 40, subject="T (#1)")
    assert github.merges == 1 and github.state == "MERGED" and not github.branch
    assert waits and waits[0] > 0
    retried = [call for call in github.calls[1:] if call[:2] == ["pr", "merge"]]
    assert all("--match-head-commit" in call for call in retried)


def test_persistent_refusal_of_a_composite_command_still_ends_as_authentication_failure():
    github = FakeGitHub(refusals=1)

    def refuse_everything(args, input=None):
        github.calls.append(list(args))
        github.refuse(args)
    with pytest.raises(TrackerError) as raised:
        GhTracker("o/r", run=refuse_everything, sleep=lambda seconds: None).merge_pr(
            2, expected_head_sha="a" * 40, subject="T")
    assert raised.value.is_auth_failure
    assert sum(call[:2] == ["pr", "merge"] for call in github.calls) == 1


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


def test_unavailable_covers_rate_limits_and_credentials_only():
    assert TrackerError("API rate limit exceeded (HTTP 403)").is_unavailable
    assert TrackerError("gh: Bad credentials (HTTP 401)").is_unavailable
    assert not TrackerError("gh: Resource not accessible by integration (HTTP 403)").is_unavailable


def test_retitle_and_pr_refresh_propagate_authentication_failures(tmp_path):
    from types import SimpleNamespace
    from foragent import run_publish

    def refuse(*args, **kwargs):
        raise TrackerError("gh: Bad credentials (HTTP 401)")
    notes = []
    ctx = SimpleNamespace(tracker=SimpleNamespace(update_pr=refuse, list_prs=refuse))
    with pytest.raises(TrackerError):
        run_publish.retitle_for_merge(ctx, 2, title="New", current_title="Old", base="main",
                                      body=lambda: "", note=notes.append)
    attempt = SimpleNamespace(ctx=ctx, target_branch="main", log=tmp_path / "log", branch="b", pr_title=None)
    with pytest.raises(TrackerError):
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
        raise TrackerError("gh: Bad credentials (HTTP 401)")
    monkeypatch.setattr(case.tracker, "merge_pr", refuse)
    assert run_issue(1, case.ctx) == "auth-failed"
    assert merges == [2], "a refused merge rejoined the merge queue"
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
        raise TrackerError(message)
    monkeypatch.setattr(board, "set_status", refuse)
    monkeypatch.setattr(board, "_order_parent", lambda ctx, n: False)
    monkeypatch.setattr(board, "_waits_on_person", lambda ctx, issue: False)
    ctx = SimpleNamespace(config=SimpleNamespace(board="PVT_x", board_map={}), tracker=object(), repo="o/r")
    if raised:
        with pytest.raises(TrackerError):
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
        raise TrackerError("gh: Bad credentials (HTTP 401)")
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
        raise TrackerError("gh: Bad credentials (HTTP 401)")
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
        raise TrackerError("gh: Bad credentials (HTTP 401)")
    monkeypatch.setattr(run_publish, "_batch_pr", refuse)
    assert run_issue(1, case.ctx) == "merged"
    assert finalized == [9] and len(case.tracker.merged_prs) == 1
    run = case.store.get_run(1)
    assert run.outcome == "merged" and "401" in run.detail
    assert len(auth_events(case)) == 1
