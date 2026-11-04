"""Boundaries of the GitHub outage wait (#1644): what counts, what the wait records, which PASS resumes."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foragent import orch_events, orch_github
from foragent.config import Config
from foragent.run_review import passed_head
from foragent.state import StateStore
from foragent.tracker import FakeTracker, GhTracker, TrackerError

from helpers.home import isolated_home  # noqa: F401
from runner_support import run_case  # noqa: F401
from helpers.github_outage import waiting_case  # noqa: F401

NOW = datetime(2026, 11, 2, 9, tzinfo=UTC)
STDOUT_503 = "HTTP/2.0 503 Service Unavailable\nx-github-request-id: ABC"


def called(stderr, stdout="", code=1):
    return subprocess.CalledProcessError(code, ["gh"], output=stdout, stderr=stderr)


def gh_failure(error, read=lambda tracker: tracker.list_prs()):
    def run(args, input=None):
        raise error
    with pytest.raises(TrackerError) as caught:
        read(GhTracker("owner/repo", run))
    return caught.value


TRAILER = "http: unexpected EOF reading trailer"


@pytest.mark.parametrize("error,waits", [
    # Anything but a definite request error waits, whatever its text (#1644 review 11).
    (called("error connecting to api.github.com: dial tcp: connection refused"), True),
    (called(f"{TRAILER}\n"), True),
    (called("a failure text no list has ever seen"), True),
    (called(""), True),
    *[(called(f"gh: HTTP {code} server error"), True) for code in (500, 502, 503, 504)],
    (called("", stdout=STDOUT_503), True),
    (OSError("gh: No such file or directory"), True),
    (subprocess.TimeoutExpired(["gh"], 120), True),
    # A definite request error: GitHub answered and refused the request.
    (called("gh: HTTP 404 Not Found"), False),
    (called("gh: HTTP 422 Validation Failed"), False),
    (called("gh: Resource not accessible by integration (HTTP 403)"), False),
    (called("", stdout="HTTP/2.0 422 Unprocessable Entity\n{}"), False),
    (called("GraphQL: Body is too long (maximum is 65536 characters) (createPullRequest)"), False),
    # stdout is data elsewhere: a failing command's JSON may name any status.
    (called("gh: HTTP 422 Validation Failed", stdout='{"message": "title HTTP 503 handling"}'), False),
])
def test_only_unreachable_or_server_errors_from_gh_wait(error, waits):
    exc = gh_failure(error)
    assert exc.waits is waits and exc.is_unavailable is waits and not exc.is_rate_limit
    # Code that builds its own message never claims gh's diagnosis.
    assert not TrackerError(str(exc)).waits


@pytest.mark.parametrize("read", [
    lambda tracker: tracker.get_issue(7), lambda tracker: tracker.pr_head_sha(7),
    lambda tracker: tracker.list_prs()], ids=["issue", "pr-head", "pr-list"])
def test_an_unrecognised_failure_waits_for_issue_and_pr_reads(read):
    assert gh_failure(called(f"{TRAILER}\n"), read).waits
    assert not gh_failure(called("gh: Not Found (HTTP 404)"), read).waits


@pytest.mark.parametrize("stderr", ["API rate limit exceeded for user (HTTP 403)", "gh: HTTP 429 Too Many Requests"])
def test_a_rate_limit_is_a_4xx_that_still_waits(stderr):
    exc = gh_failure(called(stderr))
    assert exc.waits and exc.is_rate_limit and not exc.is_unavailable


def test_a_stdout_only_server_error_is_part_of_the_error_message():
    # Every reader of str(error) (merge-wait reads, PR-update notes, run details) keeps it.
    exc = gh_failure(called("", stdout=STDOUT_503))
    assert str(exc).startswith("HTTP/2.0 503 Service Unavailable")


@pytest.mark.parametrize("stdout,code,expected", [
    ('[{"bucket": "fail", "name": "HTTP 503 retry test"}]', 1, "fail"),
    ('[{"bucket": "pending", "name": "build"}]', 8, "pending"),
])
def test_failed_checks_listing_on_stdout_is_still_read_as_checks(stdout, code, expected):
    def run(args, input=None):
        raise called("", stdout=stdout, code=code)
    assert GhTracker("owner/repo", run).pr_checks(7) == expected


def test_a_checks_read_whose_stdout_is_a_server_error_waits_instead_of_parsing_it():
    exc = gh_failure(called("", stdout=STDOUT_503 + "\n\n<html>"), lambda tracker: tracker.pr_checks(7))
    assert exc.waits


def test_a_long_message_cannot_push_the_outage_out_of_the_wait_record():
    stderr = f"failed to merge owner/{'r' * 100}#7 with subject {'S' * 255!r}: request failed"
    line = orch_github.cause(gh_failure(called(stderr, stdout=STDOUT_503)))
    assert len(line) <= 500 and line.startswith("HTTP/2.0 503 Service Unavailable")
    line = orch_github.cause(gh_failure(called(f"progress {'h' * 600}\n{TRAILER}")))
    assert len(line) <= 500 and line.startswith("progress") and line.endswith(TRAILER)


def wait_case(tmp_path, tracker=None):
    store = StateStore(tmp_path, now=lambda: NOW)
    for n in (1, 2):
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=tmp_path / f"w{n}")
    return SimpleNamespace(tracker=tracker, store=store, now=lambda: NOW)


def test_an_outage_pause_keeps_its_failure_for_every_run_that_enters_it(tmp_path):
    probes = []
    ctx = wait_case(tmp_path, SimpleNamespace(rate_limit_reset=lambda: probes.append(1)))
    until = orch_github.pause(ctx, gh_failure(called("", stdout=STDOUT_503)))
    assert until == NOW + timedelta(minutes=1) and probes == [], "the probe needs the same unreachable GitHub"
    orch_github.wait_run(ctx, 1, until)
    orch_github.wait_run(ctx, 2, ctx.store.github_wait_until(NOW))  # a run that only meets the pause
    for n in (1, 2):
        ctx.store.finish_run(n, "limit-wait")
        log, detail = ctx.store.log_path(n).read_text(), ctx.store.get_run(n).detail
        for text in (log, detail):
            assert "HTTP/2.0 503 Service Unavailable" in text and "rate limit" not in text
    assert "HTTP/2.0 503" in orch_github.wait_text(ctx.store, NOW)


def test_a_rate_limit_pause_keeps_its_probe_and_its_wording(tmp_path):
    reset = NOW + timedelta(minutes=30)
    ctx = wait_case(tmp_path, SimpleNamespace(rate_limit_reset=lambda: reset))
    orch_github.pause(ctx, gh_failure(called("", stdout=STDOUT_503)))  # an earlier outage, now a rate limit
    assert orch_github.pause(ctx, TrackerError("API rate limit exceeded (HTTP 403)")) == reset
    orch_github.wait_run(ctx, 1, reset)
    assert f"GitHub rate limit until {reset.isoformat()}" in ctx.store.log_path(1).read_text()
    assert "503" not in orch_github.wait_text(ctx.store, NOW), "the pause names its latest failure"


class Head:
    def __init__(self, sha, dirty=""):
        self.sha, self.dirty = sha, dirty

    def head_sha(self):
        return self.sha

    def _call(self, args):
        return self.dirty


@pytest.mark.parametrize("change", [
    None, "bound", "local-head", "pr-head", "body", "orchestrator", "fail", "no-pr", "dirty", "reviewer-none",
])
def test_only_a_reviewer_pass_of_the_exact_open_pr_head_and_body_resumes(tmp_path, change):
    store = StateStore(tmp_path, now=lambda: NOW)
    store.save_approval(1, body="body", body_sha="sha-1", acceptance_blobs={}, approved_by="jin",
                        approved_at=NOW.isoformat())
    tracker = FakeTracker()
    tracker.add_issue(1, "Issue", "body", ["approved"])
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="PR", body="")
    tracker.set_pr_head_sha(pr, "pushed-elsewhere" if change == "pr-head" else "reviewed")
    if change == "no-pr":
        tracker.close_pr(pr)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=lambda: NOW,
                          config=Config(reviewer="none" if change == "reviewer-none" else "claude"))
    facts = {"tree": "tree-r", "body_sha": "sha-0" if change == "body" else "sha-1"}
    if change != "bound":
        facts["head"] = "reviewed"
    orch_events.append_event(store, NOW, 1, "review", "x", verdict="FAIL" if change == "fail" else "PASS",
                             round=1, source="orchestrator" if change == "orchestrator" else "reviewer", **facts)
    if change == "bound":
        orch_events.append_event(store, NOW, 1, "review-pass-head", "bound", tree="tree-r", head="reviewed")
    head = Head("changed" if change == "local-head" else "reviewed", " M f" if change == "dirty" else "")
    assert passed_head(1, ctx, head) is (change in {None, "bound"})


def resumed(git_history, tmp_path, monkeypatch, entry="run"):
    """A run whose CI read dropped after a reviewer PASS, at the end of its wait."""
    from helpers.github_outage import CONNECTION, assert_wait, interrupt, outage_case
    from foragent.runner import review_only, run_issue

    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    recovered = case.clock[0] + timedelta(minutes=4)
    interrupt(case, monkeypatch, "pr_checks", CONNECTION, recovered)
    assert (run_issue if entry == "run" else review_only)(1, case.ctx) == "limit-wait"
    case.clock[0] = max(assert_wait(case, CONNECTION), recovered)
    return case


def reviewing(case):
    reviewed = []
    case.reviewer.steps = [lambda kwargs: reviewed.append(
        (Path(kwargs["worktree"]) / "feature.txt").read_text()) or "VERDICT: PASS"]
    return reviewed


@pytest.mark.parametrize("moved_base", [False, True])
def test_contents_a_check_changes_after_a_reused_pass_are_reviewed_before_merge(
    git_history, tmp_path, monkeypatch, moved_base,
):
    from helpers.github_outage import restart_at_wait_end
    from foragent import run_checks

    case = resumed(git_history, tmp_path, monkeypatch)
    original, calls = run_checks.run_done_when, []

    def checks(n, ctx, command, worktree, *args, **kwargs):
        calls.append(command)
        if moved_base and len(calls) == 1:
            # main moves after the resumed checks, so publication checks the new base again.
            case.history.update({"src/other.py": "VALUE = 1\n"}, message="Unrelated code")
        elif not moved_base or len(calls) > 2:
            (Path(worktree) / "feature.txt").write_text("changed by a check\n")
        return original(n, ctx, command, worktree, *args, **kwargs)

    monkeypatch.setattr(run_checks, "run_done_when", checks)
    reviewed = reviewing(case)
    assert restart_at_wait_end(case) == "merged"
    assert reviewed == ["changed by a check\n"], "Contents a check changed need their own review"


def test_a_reused_pass_that_then_fails_ci_gets_a_new_implementation_and_review(git_history, tmp_path, monkeypatch):
    from helpers.github_outage import restart_at_wait_end

    case = resumed(git_history, tmp_path, monkeypatch)
    pr = case.tracker.created_prs[0].number
    case.tracker.set_pr_checks(pr, "fail")
    case.tracker.set_pr_failed_logs(pr, "feature check failed")

    def repair(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("repaired\n")
        case.tracker.set_pr_checks(pr, "pass")
        return "Repaired"

    case.implementer.steps = [repair]
    reviewed = reviewing(case)
    assert restart_at_wait_end(case) == "merged"
    assert len(case.implementer.calls) == 2 and reviewed == ["repaired\n"]


@pytest.mark.parametrize("entry", ["run", "reviewed"])
@pytest.mark.parametrize("where", ["stderr", "stdout", "trailer"])
def test_a_server_error_from_the_merge_waits_once_and_merges_once_later(
    git_history, tmp_path, monkeypatch, entry, where,
):
    from helpers.github_outage import assert_merged_once, assert_wait, outage_case, restart_at_wait_end
    from foragent.runner import review_only

    case = resumed(git_history, tmp_path, monkeypatch) if entry == "run" else outage_case(
        git_history, tmp_path, monkeypatch, "review")
    recovered = case.clock[0] + timedelta(minutes=4)
    diagnostic = TRAILER if where == "trailer" else "HTTP/2.0 503 Service Unavailable (merge)"
    original, attempts = case.tracker.merge_pr, []

    def transport(argv, input=None):
        attempts.append(argv)
        raise called("", stdout=diagnostic) if where == "stdout" else called(diagnostic)

    github = GhTracker("owner/repo", run=transport)
    monkeypatch.setattr(case.tracker, "merge_pr", lambda pr, **kw: (
        github.merge_pr(pr, **kw) if case.clock[0] < recovered else original(pr, **kw)))
    reviews = len(case.reviewer.calls) + (entry == "reviewed")
    first = restart_at_wait_end(case) if entry == "run" else review_only(1, case.ctx)
    assert first == "limit-wait" and len(attempts) == 1, "A server error is one failed request, not a retried merge"
    until = assert_wait(case, diagnostic)
    assert case.store.github_wait_until(case.clock[0]) == until, "The wait is the shared GitHub pause"
    case.clock[0] = max(until, recovered)
    assert restart_at_wait_end(case) == "merged"
    assert_merged_once(case)
    assert len(case.reviewer.calls) == reviews, "The PASS of the merged head carries over the wait"


@pytest.mark.parametrize("own,other", [("outage", "rate-limit"), ("rate-limit", "outage")])
def test_a_wait_keeps_its_own_failure_when_another_writer_pauses_first(tmp_path, own, other):
    from foragent.run_waits import wait_for_rate_limit

    ctx = wait_case(tmp_path, SimpleNamespace(rate_limit_reset=lambda: NOW + timedelta(minutes=30)))
    errors = {"outage": gh_failure(called("", stdout=STDOUT_503)),
              "rate-limit": TrackerError("API rate limit exceeded (HTTP 403)")}
    write = ctx.store.pause_github

    def concurrent(until, cause=None):
        kept = write(until, cause)
        # Another runner's failure lands between this run's pause and its wait record.
        orch_github.pause(SimpleNamespace(**{**vars(ctx), "store": SimpleNamespace(pause_github=write)}),
                          errors[other])
        return kept

    ctx.store.pause_github = concurrent
    assert wait_for_rate_limit(1, ctx, errors[own])
    ctx.store.finish_run(1, "limit-wait")
    for text in (ctx.store.log_path(1).read_text(), ctx.store.get_run(1).detail):
        assert ("HTTP/2.0 503" in text) is (own == "outage")
        assert ("GitHub rate limit" in text) is (own == "rate-limit")
    # A run that only meets the pause later reads the latest writer's failure.
    ctx.store.pause_github = write
    orch_github.wait_run(ctx, 2, ctx.store.github_wait_until(NOW))
    assert ("HTTP/2.0 503" in ctx.store.get_run(2).detail) is (other == "outage")


@pytest.mark.parametrize("family", ["issue", "pr"])
@pytest.mark.parametrize("stream", ["stderr", "stdout", "trailer"])
def test_a_merge_wait_read_that_meets_an_outage_takes_the_shared_pause(waiting_case, family, stream):
    from helpers.github_outage import exercise

    case = waiting_case
    case.facts.fail_family = family
    case.facts.error = gh_failure({"stderr": called("gh: HTTP 503 Service Unavailable"),
                                   "trailer": called(f"{TRAILER}\n")}.get(stream) or called("", stdout=STDOUT_503))
    assert exercise(case) == "limit-wait" and len(case.facts.failed) == 1
    store = StateStore(case.store.root, now=lambda: case.now[0])
    assert store.github_wait_until(case.now[0]) is not None, "an outage waits on the shared pause"
    detail = store.get_run(1).detail
    assert (TRAILER if stream == "trailer" else "503") in detail and "rate limit" not in detail
    assert case.merge_calls == []


def test_a_gh_timeout_or_launch_failure_leads_its_record_whatever_the_arguments(tmp_path):
    long_args = ["pr", "edit", "7", "--title", "T" * 300, "--base", "b" * 300]
    timeout = gh_failure(subprocess.TimeoutExpired(["gh", *long_args], 120),
                         lambda tracker: tracker._call(long_args))
    missing = gh_failure(FileNotFoundError(2, "No such file or directory", "gh"))
    for exc, kind in ((timeout, "timed out after 120 seconds"), (missing, "could not run")):
        assert exc.waits and kind in orch_github.cause(exc)
