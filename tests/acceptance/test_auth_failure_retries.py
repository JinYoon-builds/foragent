"""Acceptance test for auth-failure-retries. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from foragent.runner import run_issue
from foragent.tracker import GhTracker
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


# Watchdogs bound a broken implementation; they do not choose its retry policy.
TRANSPORT_WATCHDOG = 20
WAIT_WATCHDOG_SECONDS = 60
AUTH_FAILURES = [
    ("git-read", "git@github.com: Permission denied (publickey).", "publickey"),
    ("git-write", "fatal: Could not read from remote repository.", "Could not read from remote repository"),
    ("github-read", "gh: Bad credentials (HTTP 401)", "401"),
    ("github-write", "gh: Bad credentials (HTTP 401)", "401"),
]


@pytest.fixture
def auth_case(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="none",
                     language="en", skill_libraries=[], test_command="true", max_retries=0)
    case.tracker.set_pr_files(2, ["feature.txt"])
    case.tracker.set_pr_checks(2, "pass")
    case.ctx.poll_hook = lambda execute, progress: execute()
    case.waits = []

    def wait(seconds):
        case.waits.append(seconds)

    def context_wait(seconds):
        wait(seconds)
        case.clock[0] += timedelta(seconds=seconds)

    # Observe requested delays without charging subprocess polling to the run's clock.
    monkeypatch.setattr(time, "sleep", wait)
    case.ctx.sleep = context_wait
    # Fixture Git can transfer only between these local repositories and runs no hooks.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    for index, (key, value) in enumerate((
        ("core.hooksPath", str(tmp_path / "no-hooks")),
        ("protocol.allow", "never"), ("protocol.file.allow", "always"),
    )):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    return case


def failing_transport(case, monkeypatch, target, message, *, persistent):
    attempts, failures = [], []

    def attempt(argv):
        attempts.append(sum(case.waits))
        assert len(attempts) <= TRANSPORT_WATCHDOG, f"{target}: authentication retries are unbounded"
        assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}, (
            "The run was blocked before its authentication retry")
        if persistent or (persistent is False and len(attempts) == 1):
            failures.append(message)
            raise subprocess.CalledProcessError(128 if argv[0] == "git" else 1,
                                                argv, stderr=message)

    git_run = subprocess.run

    def git_transport(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and list(argv[:2]) == ["git", "fetch"]:
            cwd = Path(kwargs.get("cwd") or Path.cwd()).resolve()
            reading = cwd == case.root.resolve() and list(argv[2:3]) == ["origin"]
            # Existing publication fakes emulate push with a local fetch into the origin.
            writing = cwd == case.history.origin.resolve()
            if (target == "git-read" and reading) or (target == "git-write" and writing):
                attempt(list(argv))
        return git_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", git_transport)
    find_pr, create_pr = case.tracker.find_pr, case.tracker.create_pr

    def github_transport(argv, input=None):
        attempt(["gh", *argv])
        if target == "github-read":
            assert argv[:2] == ["api", "--paginate"] and "/pulls?" in argv[2], argv
            number = find_pr("foragent/issue-1")
            return json.dumps([] if number is None else [{"number": number}])
        assert argv[:2] == ["pr", "create"], argv
        number = create_pr(head=argv[argv.index("--head") + 1],
                           base=argv[argv.index("--base") + 1],
                           title=argv[argv.index("--title") + 1], body=input)
        return f"https://github.com/owner/repo/pull/{number}\n"

    github = GhTracker("owner/repo", run=github_transport)
    if target == "github-read":
        monkeypatch.setattr(case.tracker, "find_pr", github.find_pr)
    elif target == "github-write":
        monkeypatch.setattr(case.tracker, "create_pr", github.create_pr)
    return SimpleNamespace(attempts=attempts, failures=failures)


@pytest.mark.parametrize("target,message,marker", AUTH_FAILURES)
def test_available_transports_complete_the_run_without_duplicate_effects(
    auth_case, monkeypatch, target, message, marker,
):
    case = auth_case
    transport = failing_transport(case, monkeypatch, target, message, persistent=None)
    assert run_issue(1, case.ctx) == "merged", f"{target}: available transport did not complete"
    assert transport.attempts and transport.failures == []
    assert len(case.implementer.calls) == len(case.tracker.created_prs) == 1
    assert case.tracker.merged_prs == [2] and case.store.load_flag(1) is None


@pytest.mark.parametrize("target,message,marker", AUTH_FAILURES)
def test_one_authentication_failure_waits_then_the_run_continues(
    auth_case, monkeypatch, target, message, marker,
):
    case = auth_case
    transport = failing_transport(case, monkeypatch, target, message, persistent=False)
    outcome = run_issue(1, case.ctx)
    assert outcome == "merged", f"{target}: transient authentication failure ended the run as {outcome}"
    assert transport.failures == [message]
    assert len(transport.attempts) >= 2
    assert transport.attempts[1] > transport.attempts[0], "Authentication retry did not wait"
    assert transport.attempts[1] - transport.attempts[0] <= WAIT_WATCHDOG_SECONDS
    assert len(case.implementer.calls) == 1, "Transport recovery repeated implementation work"
    assert len(case.tracker.created_prs) == 1 and case.tracker.merged_prs == [2]
    assert case.tracker.get_issue(1).state == "closed"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human", "running"}
    assert case.store.load_flag(1) is None
    assert len([run for run in case.store.read_history() if run.number == 1]) == 1


@pytest.mark.parametrize("target,message,marker", AUTH_FAILURES)
def test_persistent_authentication_failure_is_bounded_and_reports_one_reason(
    auth_case, monkeypatch, target, message, marker,
):
    case = auth_case
    transport = failing_transport(case, monkeypatch, target, message, persistent=True)
    outcome = run_issue(1, case.ctx)
    assert 2 <= len(transport.attempts) <= TRANSPORT_WATCHDOG, (
        f"{target}: expected bounded authentication retries, got {len(transport.attempts)} attempt(s)")
    assert all(later > earlier for earlier, later in zip(transport.attempts, transport.attempts[1:])), (
        "Authentication retries did not wait")
    assert transport.attempts[-1] - transport.attempts[0] <= WAIT_WATCHDOG_SECONDS, (
        "Authentication retry waits exceeded the test watchdog")
    run = case.store.get_run(1)
    assert run.status == "finished" and outcome != "merged", run
    assert run.detail and len(run.detail.splitlines()) == 1 and marker in run.detail, run.detail
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human", "running"}
    assert case.store.load_flag(1) is None
    assert not case.tracker.created_prs and not case.tracker.merged_prs
    assert len([past for past in case.store.read_history() if past.number == 1]) == 1


def test_non_authentication_git_failure_keeps_its_existing_failure_behavior(auth_case, monkeypatch):
    case = auth_case
    message = "fatal: couldn't find remote ref missing-branch"
    transport = failing_transport(case, monkeypatch, "git-read", message, persistent=True)
    assert run_issue(1, case.ctx) == "error"
    assert len(transport.attempts) == 1, "Unrelated Git errors entered authentication recovery"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert message in case.store.get_run(1).detail
