"""Acceptance test for github-limit-wait. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json
import subprocess

from test_local_view import github_reply, record_gh
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home


def github_case(git_history, tmp_path, monkeypatch, *, status=403, header=True,
                reset_minutes=10, attempts=3):
    """Real GhTracker reads/errors, offline writes, and the existing runner fixture."""
    from foragent.tracker import GhTracker

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[],
                     test_command="true", lanes={"orch": {"paths": ["**"]}},
                     max_runtime_minutes=1, stall_minutes=1)
    case.tracker.add_labels(1, ["lane:orch"])
    case.tracker.record_label(1, "approved", "human")
    case.store.update_run(1, lane="orch", attempts=attempts, restarts=1)
    case.reset = case.clock[0] + timedelta(minutes=reset_minutes)
    fallback_reset = case.clock[0] + timedelta(minutes=10)
    case.error_pending = True
    case.errors = []
    case.mutations = []
    case.runner_calls = []

    def gh_run(argv, input=None):
        case.runner_calls.append(["gh", *argv])
        return subprocess.run(["gh", *argv], input=input, check=True,
                              capture_output=True, text=True, timeout=120).stdout

    case.github = GhTracker("owner/repo", run=gh_run)

    # Keep writes in FakeTracker, but count them separately: an offline tick
    # must neither read GitHub nor silently flag an issue through the fake.
    def offline_write(method):
        def write(*args, **kwargs):
            case.mutations.append((method, args, kwargs))
            return getattr(case.tracker, method)(*args, **kwargs)
        return write

    for method in ("add_labels", "remove_labels", "comment", "close_issue",
                   "create_pr", "update_pr", "merge_pr"):
        monkeypatch.setattr(case.github, method, offline_write(method))

    def reply(argv):
        if argv[0] == "api" and "rate_limit" in argv:
            window = {"limit": 5000, "remaining": 0, "used": 5000,
                      "reset": int(fallback_reset.timestamp())}
            return json.dumps({"resources": {"core": window, "graphql": window}, "rate": window})
        if argv == ["api", "repos/owner/repo/issues/1"] and case.error_pending:
            case.error_pending = False
            message = ("gh: API rate limit exceeded for offline-user "
                       f"(HTTP {status})" if status in {403, 429}
                       else f"gh: HTTP {status} Validation Failed")
            case.errors.append(message)
            output = (f"HTTP/2 {status}\r\nX-RateLimit-Reset: {int(case.reset.timestamp())}\r\n\r\n"
                      if header else "")
            # GhTracker converts this into the real TrackerError, including
            # stdout carrying the response header when it is available.
            raise subprocess.CalledProcessError(1, ["gh", *argv], output=output, stderr=message)
        if argv[:2] == ["api", "graphql"]:
            return json.dumps({"data": {"repository": {"issue": {
                "lastEditedAt": case.tracker.body_last_edited_at(1),
            }}}})
        if argv[:2] == ["pr", "checks"]:
            return "[]"
        endpoint = next((arg for arg in argv if arg.startswith("repos/owner/repo/pulls/")), None)
        if argv[0] == "api" and endpoint is not None:
            parts = endpoint.partition("?")[0].removeprefix("repos/owner/repo/pulls/").split("/")
            number = int(parts[0])
            if parts[1:] == ["files"]:
                return json.dumps([{"filename": path} for path in case.tracker.pr_changed_files(number)])
            if len(parts) == 1:
                return json.dumps({"head": {"sha": case.tracker.pr_head_sha(number)}})
        return github_reply(case, argv)

    case.gh_calls = record_gh(monkeypatch, reply=reply)
    case.ctx.tracker = case.github
    return case


def rate_limit_calls(case):
    return [argv for argv in case.runner_calls if "rate_limit" in argv]


def test_github_limit_wait_403_records_header_reset_without_failure_or_attempt_cost(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    case = github_case(git_history, tmp_path, monkeypatch, attempts=1)
    outcome = run_issue(1, case.ctx)
    assert len(case.errors) == 1, "The real tracker boundary must raise the rate-limit error"
    # Reopen the store so a future scheduler process can see the wait too.
    persisted = StateStore(case.store.root, now=lambda: case.clock[0])
    run = persisted.get_run(1)
    assert run.waiting_until is not None, "HTTP 403 rate limits must persist a wait instead of failing"
    assert datetime.fromisoformat(run.waiting_until) == case.reset
    assert outcome not in {"error", "retries-exceeded", "blocked"}
    assert (run.attempts, run.restarts) == (1, 1)
    assert persisted.load_flag(1) is None
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert rate_limit_calls(case) == [], "An error carrying X-RateLimit-Reset needs no extra GitHub read"
    assert case.implementer.calls == case.reviewer.calls == []


def test_github_limit_wait_429_uses_tracker_runner_for_missing_reset_and_keeps_attempts(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case = github_case(git_history, tmp_path, monkeypatch, status=429, header=False)
    outcome = run_issue(1, case.ctx)
    assert len(case.errors) == 1
    assert len(rate_limit_calls(case)) == 1, "Missing headers require gh api rate_limit via the tracker runner"
    run = case.store.get_run(1)
    assert run.waiting_until is not None
    assert datetime.fromisoformat(run.waiting_until) == case.reset
    assert outcome not in {"error", "retries-exceeded", "blocked"}
    assert (run.attempts, run.restarts) == (3, 1)
    assert case.store.load_flag(1) is None
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def test_github_limit_wait_tick_is_offline_before_reset_then_restarts_and_merges(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue
    from foragent.scheduler import tick
    from foragent.state import StateStore

    case = github_case(git_history, tmp_path, monkeypatch, header=False)
    run_issue(1, case.ctx)
    assert len(case.errors) == 1
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    launches = []
    ctx = tick_context(case)
    ctx.tracker = case.github
    ctx.launcher = lambda number: launches.append(number) or 7000 + number
    mutations = list(case.mutations)
    case.gh_calls.clear()
    case.clock[0] = case.reset - timedelta(seconds=1)
    tick(ctx)
    assert case.gh_calls == [], "A tick before GitHub reset must make zero gh calls"
    assert launches == [] and case.mutations == mutations
    assert case.store.load_flag(1) is None
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert case.store.get_run(1).attempts == 3
    assert case.implementer.calls == case.reviewer.calls == []

    case.clock[0] = case.reset
    tick(ctx)
    assert launches == [1], "The first tick at reset must make the waiting issue runnable again"
    assert case.gh_calls, "GitHub reads must resume after the reset"
    assert case.store.get_run(1).attempts == 3, "Restarting must preserve already consumed attempts"
    assert run_issue(1, case.ctx) == "merged"
    assert case.store.get_run(1).attempts == 3
    assert case.tracker.get_issue(1).state == "closed"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert len(case.tracker.merged_prs) == 1
    assert case.store.load_flag(1) is None


def test_github_limit_wait_past_thirty_minutes_does_not_escalate_to_human(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue
    from foragent.scheduler import tick

    case = github_case(git_history, tmp_path, monkeypatch, reset_minutes=45)
    assert case.config.orchestrator_timeout_minutes == 30
    run_issue(1, case.ctx)
    assert len(case.errors) == 1
    case.clock[0] += timedelta(minutes=31)
    launches, killed = [], []
    ctx = tick_context(case)
    ctx.tracker = case.github
    ctx.launcher = lambda number: launches.append(number) or 7000 + number
    ctx.kill_runner = killed.append
    case.gh_calls.clear()
    report = tick(ctx)
    assert "needs-human" not in case.tracker.get_issue(1).labels, (
        "A GitHub reset wait is not an overdue orchestrator blocker"
    )
    assert "blocked" not in case.tracker.get_issue(1).labels
    assert not any("orchestrator-timeout" in comment.body for comment in case.tracker.list_comments(1))
    assert not any("orchestrator-timeout" in action for action in report.actions)
    assert launches == killed == case.gh_calls == []
    assert case.store.load_flag(1) is None
    run = case.store.get_run(1)
    assert run.waiting_until is not None
    assert datetime.fromisoformat(run.waiting_until) == case.reset
    assert (run.attempts, run.restarts) == (3, 1)


def test_github_limit_wait_422_validation_error_still_fails_without_a_reset_probe(
    git_history, tmp_path, monkeypatch,
):
    from foragent.reasons import Reason
    from foragent.runner import run_issue

    case = github_case(git_history, tmp_path, monkeypatch, status=422, header=False)
    assert run_issue(1, case.ctx) == "error"
    assert case.errors == ["gh: HTTP 422 Validation Failed"]
    run = case.store.get_run(1)
    assert run.status == "finished" and run.outcome == "error"
    assert run.waiting_until is None and run.attempts == 3
    assert case.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "HTTP 422 Validation Failed" in case.tracker.list_comments(1)[-1].body
    assert not case.store.lane_paused("orch", case.clock[0])
    assert rate_limit_calls(case) == []
    assert case.tracker.merged_prs == []
