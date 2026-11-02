"""Boundaries of the GitHub outage wait (#1644): what counts, what it records, which PASS resumes."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foragent import gh, orch_events, orch_github
from foragent.config import Config
from foragent.run_review import passed_head
from foragent.state import StateStore
from foragent.tracker import FakeTracker, GhTracker, TrackerError

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_skills_autosync import isolated_home  # noqa: E402,F401

NOW = datetime(2026, 11, 2, 9, tzinfo=UTC)


def gh_failure(error):
    def run(args, input=None):
        raise error
    try:
        GhTracker("owner/repo", run).list_prs()
    except TrackerError as exc:
        return exc
    raise AssertionError("gh did not fail")


def called(stderr, stdout=""):
    return subprocess.CalledProcessError(1, ["gh"], output=stdout, stderr=stderr)


@pytest.mark.parametrize("error,waits", [
    (called("error connecting to api.github.com: dial tcp: connection refused"), True),
    (called("read tcp 10.0.0.2:5000->140.82.112.6:443: read: connection reset by peer"), True),
    (called("Post https://api.github.com/graphql: net/http: TLS handshake timeout"), True),
    (called("dial tcp: lookup api.github.com: i/o timeout"), True),
    *[(called(f"gh: HTTP {code} server error"), True) for code in (500, 502, 503, 504)],
    (called("", stdout="HTTP/2.0 503 Service Unavailable\nContent-Type: text/html"), True),
    # Elsewhere stdout is data: a failing command's JSON may name any status.
    (called("gh: HTTP 422 Validation Failed", stdout='{"message": "title HTTP 503 handling"}'), False),
    (called("", stdout='[{"bucket": "fail", "name": "HTTP 503 retry test"}]'), False),
    (OSError("gh: No such file or directory"), True),
    (subprocess.TimeoutExpired(["gh"], 120), True),
    (called("gh: HTTP 404 Not Found"), False),
    (called("gh: HTTP 422 Validation Failed"), False),
    (called("gh: HTTP 403 Resource not accessible by integration"), False),
    (called("gh: HTTP 401 Bad credentials"), False),
    (called("GraphQL: Could not resolve to a PullRequest with the number of 5000."), False),
])
def test_only_unreachable_or_server_errors_from_gh_wait(error, waits):
    exc = gh_failure(error)
    assert exc.is_unavailable is waits and exc.waits is waits
    assert exc.is_rate_limit is False


def test_outage_is_classified_only_where_gh_output_becomes_an_error():
    # Code that builds its own message never claims gh's diagnosis, even when it quotes one.
    assert not TrackerError("gh: HTTP 502 Bad Gateway from pulls endpoint").waits
    assert not TrackerError("issue #1 already has a running execution").waits
    assert TrackerError("API rate limit exceeded (HTTP 403)").waits  # rate limits keep their reading


def test_a_spent_tick_deadline_is_not_an_outage(monkeypatch):
    def exhausted(limit):
        raise gh.DeadlineExhausted("no time left")

    monkeypatch.setattr(gh, "call_timeout", exhausted)
    with pytest.raises(TrackerError) as error:
        GhTracker("owner/repo", lambda args, input=None: "[]").list_prs()
    assert str(error.value).startswith("deferred:") and not error.value.waits


def test_outage_waits_one_minute_without_a_probe_and_logs_its_diagnosis(tmp_path):
    probes = []
    tracker = SimpleNamespace(rate_limit_reset=lambda: probes.append(1) or NOW + timedelta(hours=1))
    store = StateStore(tmp_path, now=lambda: NOW)
    ctx = SimpleNamespace(tracker=tracker, store=store, now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "w")
    error = gh_failure(called("gh: HTTP 503 Service Unavailable"))
    until = orch_github.pause(ctx, error)
    assert until == NOW + timedelta(minutes=1) and probes == []
    assert StateStore(tmp_path).github_wait_until(NOW) == until  # the shared project pause
    assert orch_github.wait_run(ctx, 1, until, error) == "limit-wait"
    log = store.log_path(1).read_text()
    assert "GitHub unavailable (gh: HTTP 503 Service Unavailable): waiting until" in log
    assert "rate limit" not in log
    run = store.get_run(1)
    assert run.outcome == "limit-wait" and run.waiting_until == until.isoformat()


def test_rate_limit_wait_keeps_its_probe_and_wording(tmp_path):
    probes = []
    reset = NOW + timedelta(minutes=30)
    tracker = SimpleNamespace(rate_limit_reset=lambda: probes.append(1) or reset)
    store = StateStore(tmp_path, now=lambda: NOW)
    ctx = SimpleNamespace(tracker=tracker, store=store, now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "w")
    error = TrackerError("API rate limit exceeded (HTTP 403)")
    assert orch_github.pause(ctx, error) == reset and probes == [1]
    orch_github.wait_run(ctx, 1, reset, error)
    assert f"GitHub rate limit: waiting until {reset.isoformat()}" in store.log_path(1).read_text()


class Head:
    def __init__(self, sha, dirty=""):
        self.sha, self.dirty = sha, dirty

    def head_sha(self):
        return self.sha

    def _call(self, args):
        assert args[0] == "status"
        return self.dirty


def pass_case(tmp_path, *, reviewer="claude"):
    store = StateStore(tmp_path, now=lambda: NOW)
    store.save_approval(1, body="body", body_sha="sha-1", acceptance_blobs={}, approved_by="jin",
                        approved_at=NOW.isoformat())
    tracker = FakeTracker()
    tracker.add_issue(1, "Issue", "body", ["approved"])
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="PR", body="")
    tracker.set_pr_head_sha(pr, "reviewed")
    ctx = SimpleNamespace(store=store, tracker=tracker, now=lambda: NOW,
                          config=Config(reviewer=reviewer))
    return ctx, pr


def record_pass(ctx, *, head="reviewed", body_sha="sha-1", source="reviewer", verdict="PASS", bound=False):
    facts = {"tree": f"tree-{head}", "body_sha": body_sha}
    if not bound:
        facts["head"] = head
    orch_events.append_event(ctx.store, NOW, 1, "review", f"{verdict} 0 findings", verdict=verdict,
                             findings=0, round=1, source=source, **facts)
    if bound:
        orch_events.append_event(ctx.store, NOW, 1, "review-pass-head", "bound", tree=f"tree-{head}", head=head)


@pytest.mark.parametrize("bound", [False, True])
def test_a_reviewer_pass_of_the_exact_open_pr_head_resumes(tmp_path, bound):
    ctx, _ = pass_case(tmp_path)
    record_pass(ctx, bound=bound)
    assert passed_head(1, ctx, Head("reviewed")) is True


@pytest.mark.parametrize("change", [
    "local-head", "pr-head", "body", "orchestrator", "fail", "no-pr", "dirty", "reviewer-none", "other-issue",
])
def test_any_change_since_the_pass_needs_a_new_review(tmp_path, change):
    ctx, pr = pass_case(tmp_path, reviewer="none" if change == "reviewer-none" else "claude")
    record_pass(ctx, body_sha="sha-0" if change == "body" else "sha-1",
                source="orchestrator" if change == "orchestrator" else "reviewer",
                verdict="FAIL" if change == "fail" else "PASS")
    if change == "other-issue":
        ctx.store.save_approval(2, body="b", body_sha="sha-1", acceptance_blobs={}, approved_by="jin",
                                approved_at=NOW.isoformat())
        assert passed_head(2, ctx, Head("reviewed")) is False
        return
    if change == "pr-head":
        ctx.tracker.set_pr_head_sha(pr, "pushed-elsewhere")
    if change == "no-pr":
        ctx.tracker.close_pr(pr)
    head = Head("changed" if change == "local-head" else "reviewed", " M feature.txt" if change == "dirty" else "")
    assert passed_head(1, ctx, head) is False


def test_a_reused_pass_that_then_fails_ci_gets_a_new_implementation_and_review(
    git_history, tmp_path, monkeypatch,
):
    from test_github_drop import CONNECTION, assert_wait, interrupt, outage_case, restart_at_wait_end
    from foragent.runner import run_issue

    case = outage_case(git_history, tmp_path, monkeypatch, "run")
    recovered = case.clock[0] + timedelta(minutes=4)
    interrupt(case, monkeypatch, "pr_checks", CONNECTION, recovered)
    assert run_issue(1, case.ctx) == "limit-wait"
    until = assert_wait(case, CONNECTION)
    pr = case.tracker.created_prs[0].number
    case.tracker.set_pr_checks(pr, "fail")
    case.tracker.set_pr_failed_logs(pr, "feature check failed")

    def repair(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("repaired\n")
        case.tracker.set_pr_checks(pr, "pass")
        return "Repaired"

    case.implementer.steps = [repair]
    case.reviewer.steps = ["VERDICT: PASS"]
    case.clock[0] = max(until, recovered)
    assert restart_at_wait_end(case) == "merged"
    # The PASS covered the old head only: the CI repair is implemented and reviewed again.
    assert len(case.implementer.calls) == 2 and len(case.reviewer.calls) == 2


def test_checks_that_change_the_reused_head_send_the_new_contents_to_review(tmp_path):
    from foragent.run_review import unchanged_since_pass

    class Tree:
        def __init__(self, head, staged, committed="t1"):
            self.head, self.staged, self.committed = head, staged, committed

        def head_sha(self):
            return self.head

        def staged_tree_sha(self):
            return self.staged

        def _call(self, args):
            assert args == ["rev-parse", "HEAD^{tree}"]
            return self.committed + "\n"

    assert unchanged_since_pass(Tree("reviewed", "t1"), "reviewed") is True
    assert unchanged_since_pass(Tree("reviewed", "t2"), "reviewed") is False  # a check edited tracked files
    assert unchanged_since_pass(Tree("merged-base", "t1"), "reviewed") is False  # follow_base merged the base


def drop_case(git_history, tmp_path, monkeypatch, entry="run"):
    from test_github_drop import CONNECTION, assert_wait, interrupt, outage_case

    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    recovered = case.clock[0] + timedelta(minutes=4)
    interrupt(case, monkeypatch, "pr_checks", CONNECTION, recovered)
    from foragent.runner import review_only, run_issue

    assert (run_issue if entry == "run" else review_only)(1, case.ctx) == "limit-wait"
    case.clock[0] = max(assert_wait(case, CONNECTION), recovered)
    return case


def test_a_done_when_edit_after_a_reused_pass_is_reviewed_before_merge(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from test_github_drop import restart_at_wait_end
    from foragent import run_checks

    case = drop_case(git_history, tmp_path, monkeypatch)
    original = run_checks.run_done_when

    def editing(n, ctx, command, worktree, *args, **kwargs):
        (Path(worktree) / "feature.txt").write_text("changed by Done when\n")
        return original(n, ctx, command, worktree, *args, **kwargs)

    monkeypatch.setattr(run_checks, "run_done_when", editing)
    reviewed = []

    def review(kwargs):
        reviewed.append((Path(kwargs["worktree"]) / "feature.txt").read_text())
        return "VERDICT: PASS"

    case.reviewer.steps = [review]
    assert restart_at_wait_end(case) == "merged"
    assert reviewed == ["changed by Done when\n"], "Contents a check changed need their own review"
    head = case.tracker.pr_head_sha(case.tracker.created_prs[0].number)
    assert git_command(case.history.origin, "show", f"{head}:feature.txt") == "changed by Done when"


@pytest.mark.parametrize("entry", ["run", "review"])
@pytest.mark.parametrize("where", ["stderr", "stdout"])
def test_a_server_error_from_the_merge_waits_and_merges_once_later(
    git_history, tmp_path, monkeypatch, entry, where,
):
    from test_github_drop import assert_merged_once, assert_wait, restart_at_wait_end

    case = drop_case(git_history, tmp_path, monkeypatch, entry)
    recovered = case.clock[0] + timedelta(minutes=4)
    diagnostic = "HTTP/2.0 503 Service Unavailable (merge)"
    original, attempts = case.tracker.merge_pr, []

    def transport(argv, input=None):
        attempts.append(argv)
        stream = {"stderr": diagnostic} if where == "stderr" else {"stderr": "", "output": diagnostic}
        raise subprocess.CalledProcessError(1, ["gh", *argv], **stream)

    github = GhTracker("owner/repo", run=transport)

    def merge(pr, **kwargs):
        if case.clock[0] < recovered:
            return github.merge_pr(pr, **kwargs)
        return original(pr, **kwargs)

    monkeypatch.setattr(case.tracker, "merge_pr", merge)
    reviews = len(case.reviewer.calls)
    assert restart_at_wait_end(case) == "limit-wait"
    assert len(attempts) == 1, "A server error is one failed request, not a retried merge"
    until = assert_wait(case, diagnostic)  # the actual diagnosis, even when gh printed it on stdout
    assert case.store.github_wait_until(case.clock[0]) == until, "The wait is the shared GitHub pause"
    case.clock[0] = max(until, recovered)
    assert restart_at_wait_end(case) == "merged"
    assert_merged_once(case)
    assert len(case.reviewer.calls) == reviews


@pytest.mark.parametrize("where", ["stderr", "stdout"])
def test_a_server_error_from_the_reviewed_merge_waits_and_resumes_without_a_new_review(
    git_history, tmp_path, monkeypatch, where,
):
    from test_github_drop import assert_merged_once, assert_wait, outage_case, restart_at_wait_end
    from foragent.runner import review_only

    case = outage_case(git_history, tmp_path, monkeypatch, "review")
    recovered = case.clock[0] + timedelta(minutes=4)
    diagnostic = "HTTP/1.1 502 Bad Gateway (reviewed merge)"
    original, attempts = case.tracker.merge_pr, []

    def transport(argv, input=None):
        attempts.append(argv)
        stream = {"stderr": diagnostic} if where == "stderr" else {"stderr": "", "output": diagnostic}
        raise subprocess.CalledProcessError(1, ["gh", *argv], **stream)

    github = GhTracker("owner/repo", run=transport)
    monkeypatch.setattr(case.tracker, "merge_pr", lambda pr, **kw: (
        github.merge_pr(pr, **kw) if case.clock[0] < recovered else original(pr, **kw)))
    assert review_only(1, case.ctx) == "limit-wait"
    assert len(attempts) == 1 and len(case.reviewer.calls) == 1
    until = assert_wait(case, diagnostic)
    case.clock[0] = max(until, recovered)
    assert restart_at_wait_end(case) == "merged"
    assert_merged_once(case)
    assert len(case.reviewer.calls) == 1, "The reviewed head's PASS carries over the wait"


@pytest.mark.parametrize("stdout,expected", [
    ('[{"bucket": "fail", "name": "HTTP 503 retry test"}]', "fail"),
    ('[{"bucket": "pending", "name": "build"}]', "pending"),
])
def test_failed_checks_listing_on_stdout_is_still_read_as_checks(stdout, expected):
    def run(args, input=None):
        raise subprocess.CalledProcessError(1 if expected == "fail" else 8, ["gh"], output=stdout, stderr="")
    assert GhTracker("owner/repo", run).pr_checks(7) == expected


def test_a_checks_read_whose_stdout_is_a_server_error_waits_instead_of_parsing_it():
    def run(args, input=None):
        raise subprocess.CalledProcessError(1, ["gh"], output="HTTP/2.0 503 Service Unavailable\n\n<html>", stderr="")
    with pytest.raises(TrackerError) as error:
        GhTracker("owner/repo", run).pr_checks(7)
    assert error.value.waits and "HTTP/2.0 503" in orch_github.cause(error.value)


def test_a_long_message_cannot_push_the_outage_out_of_the_wait_record():
    subject = "S" * 255
    stderr = f"failed to merge owner/{'r' * 100}#7 with subject {subject!r}: request failed"
    exc = gh_failure(called(stderr, stdout="HTTP/2.0 503 Service Unavailable\nx-github-request-id: ABC"))
    assert exc.waits
    line = orch_github.cause(exc)
    assert len(line) <= 500 and line.startswith("HTTP/2.0 503 Service Unavailable")
    assert "failed to merge" in line  # the rest of gh's message still follows
    long_line = gh_failure(called(f"error connecting to {'h' * 600}.example: dial tcp: connection refused"))
    assert "connection refused" in orch_github.cause(long_line)


def test_a_done_when_edit_on_a_moved_base_after_a_reused_pass_is_reviewed(
    git_history, tmp_path, monkeypatch,
):
    from test_github_drop import restart_at_wait_end
    from foragent import run_checks

    case = drop_case(git_history, tmp_path, monkeypatch)
    original, calls = run_checks.run_done_when, []

    def checks(n, ctx, command, worktree, *args, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            # main moves after the resumed checks started, so publication refreshes the base.
            case.history.update({"src/other.py": "VALUE = 1\n"}, message="Unrelated code")
        elif len(calls) > 2:
            (Path(worktree) / "feature.txt").write_text("changed on the moved base\n")
        return original(n, ctx, command, worktree, *args, **kwargs)

    monkeypatch.setattr(run_checks, "run_done_when", checks)
    reviewed = []

    def review(kwargs):
        reviewed.append((Path(kwargs["worktree"]) / "feature.txt").read_text())
        return "VERDICT: PASS"

    case.reviewer.steps = [review]
    assert restart_at_wait_end(case) == "merged"
    assert len(calls) > 2, "the moved base was verified again"
    assert reviewed == ["changed on the moved base\n"], "Contents a refresh check changed need their own review"
