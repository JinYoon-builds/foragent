"""Acceptance test for star-prompt. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home


# Chosen public names: brief.STAR_PROMPT_MERGE_THRESHOLD = 10,
# fa brief --star ANSWER, and <machine data>/star-prompt.json.
# Only the exact answer "yes" consents; "no", "dismiss", an empty answer,
# and closing the first eligible screen without answering are all final.
# The target is Foragent itself, never the repository being worked on.
FORAGENT_REPO = "JinYoon-builds/foragent"
STAR_PUT = ["gh", "api", "-X", "PUT", f"user/starred/{FORAGENT_REPO}"]


@pytest.fixture(autouse=True)
def offline_star_environment(tmp_path, monkeypatch, isolated_home):
    """Use the recorded-command seam; no real gh, models, signals or network."""
    from foragent import capacity, executors

    machine = isolated_home / ".local/share/foragent"
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    monkeypatch.setenv("GH_CONFIG_DIR", str(isolated_home / ".config/gh"))
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("star-prompt tests must not use real network, signals or model turns")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for module, name in ((os, "kill"), (os, "killpg"), (signal, "raise_signal"),
                         (signal, "pthread_kill"), (signal, "pidfd_send_signal")):
        monkeypatch.setattr(module, name, forbidden, raising=False)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)

    popen = subprocess.Popen

    def local_git_only(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv and argv[0] == "git", (
            f"Only fixture Git may start a process; gh must use record_gh: {argv}")
        assert Path(kwargs.get("cwd", Path.cwd())).resolve().is_relative_to(tmp_path.resolve()), argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git_only)
    gh = SimpleNamespace(auth="authenticated", machine=machine)

    def reply(argv):
        if argv == ["auth", "status"]:
            if gh.auth == "missing":
                raise FileNotFoundError("offline fixture: gh is not installed")
            if gh.auth == "logged-out":
                raise subprocess.CalledProcessError(
                    1, ["gh", *argv], output="", stderr="Not logged into any GitHub hosts")
            return "Logged in to github.com as offline-user\n"
        if ["gh", *argv] == STAR_PUT:
            return ""
        pytest.fail(f"Unexpected gh command; merge counts must come from local records: {argv}")

    gh.calls = record_gh(monkeypatch, reply=reply)
    recorded_run = subprocess.run

    def run(argv, *args, **kwargs):
        # record_gh replies raise for failures. Preserve real run(check=False)
        # semantics too, so either normal gh-auth calling convention is testable.
        try:
            return recorded_run(argv, *args, **kwargs)
        except subprocess.CalledProcessError as exc:
            if (isinstance(argv, (list, tuple)) and argv and argv[0] == "gh"
                    and not kwargs.get("check", False)):
                return subprocess.CompletedProcess(argv, exc.returncode, exc.stdout, exc.stderr)
            raise

    monkeypatch.setattr(subprocess, "run", run)
    return gh


@pytest.fixture
def star_case(git_history, tmp_path, monkeypatch, offline_star_environment):
    from foragent.state import StateStore

    case = make_case(git_history, tmp_path, monkeypatch, language="en", skill_libraries=[],
                     notify="none", test_command="true")
    case.clock[0] = datetime(2026, 10, 25, 8, tzinfo=UTC)
    # Keep the production machine/per-repository layout. bind_cli normally
    # redirects every repository to one store, which would hide scope bugs here.
    case.store_factory = StateStore.for_repo
    case.store = case.ctx.store = case.store_factory(case.config.repo, now=case.ctx.now)
    case.tracker.close_issue(1)
    case.main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: case.store_factory(repo, now=case.ctx.now)))
    case.gh = offline_star_environment
    return case


def record_finish(case, number, *, store=None, outcome="merged"):
    """Seed the gate's real persistent completion records without a worker turn."""
    store = store if store is not None else case.store
    case.clock[0] += timedelta(seconds=1)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.finish_run(number, outcome=outcome)


def record_merges(case, numbers, *, store=None):
    for number in numbers:
        record_finish(case, number, store=store)


def switch_repository(case, repo):
    case.config = case.ctx.config = replace(case.ctx.config, repo=repo)
    case.ctx.repo = repo
    case.store = case.ctx.store = case.store_factory(repo, now=case.ctx.now)


def command(case, capsys, *argv):
    capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def briefing(case, capsys):
    code, output, error = command(case, capsys, "brief")
    assert code == 0, f"Human briefing must succeed: {error}"
    return output


def star_lines(text):
    # State paths can contain this test's name, and an acknowledgement may name
    # a star. Match the human question, not those incidental strings.
    return [line for line in text.splitlines()
            if re.search(r"(?<![\w/.-])star(?![\w/.-])", line, re.I) and "?" in line]


def assert_offer(text, reason):
    lines = star_lines(text)
    assert len(lines) == 1, f"{reason}: expected one star question, got {lines!r} in {text!r}"
    line = lines[0]
    assert FORAGENT_REPO.casefold() in line.casefold(), "Name Foragent's repository in the question"
    assert "?" in line, "Ask whether to star; do not announce or assume consent"
    for answer in ("yes", "no"):
        assert re.search(rf"\b{answer}\b", line, re.I), f"Show {answer!r} on the same question line"
    assert not re.search(r"\blater\b", line, re.I), "Offer yes/no without a deferral option"


def assert_no_offer(text, reason):
    assert star_lines(text) == [], f"{reason}: unexpected star question in {text!r}"


def answer(case, capsys, choice):
    code, output, error = command(case, capsys, "brief", "--star", choice)
    assert code == 0, f"fa brief --star {choice} must accept the explicit answer: {error}"
    return output


def assert_machine_record(case):
    path = case.gh.machine / "star-prompt.json"
    assert path.is_file(), "Persist the one-time prompt in machine data, outside any repository's state"
    assert json.loads(path.read_text(encoding="utf-8")), "The machine prompt record must be durable JSON"
    assert not (case.root / "star-prompt.json").exists()


def test_star_prompt_waits_for_ten_distinct_local_gate_merges(star_case, capsys):
    from foragent import brief

    case = star_case
    assert_no_offer(briefing(case, capsys), "A new installation has not delivered value yet")
    # Neither failed work, active work, nor an observed remote merge proves a
    # merge completed by this machine's gate.
    for number, outcome in enumerate(("blocked", "crashed", "check-failed"), 100):
        record_finish(case, number, outcome=outcome)
    case.store.start_run(200, branch="foragent/issue-200", worktree=case.store.worktree_path(200))
    case.store.update_local_issue(300, title="Merged elsewhere", state="closed", merged=True, pr_number=301)
    record_merges(case, range(1, 10))
    # Repeated executions of one already merged issue are still one issue.
    record_finish(case, 1)
    assert_no_offer(briefing(case, capsys), "Nine distinct local merges are below the threshold")
    assert all(call == ["gh", "auth", "status"] for call in case.gh.calls)

    record_finish(case, 10)
    assert_offer(briefing(case, capsys), "The tenth local gate merge must unlock the human request")
    assert getattr(brief, "STAR_PROMPT_MERGE_THRESHOLD", None) == 10, (
        "Name the threshold foragent.brief.STAR_PROMPT_MERGE_THRESHOLD and default it to 10")
    assert STAR_PUT not in case.gh.calls, "Displaying a question is not consent to star"


def test_star_prompt_counts_machine_merges_across_repositories(star_case, capsys):
    case = star_case
    second = case.store_factory("owner/second", now=case.ctx.now)
    record_merges(case, range(1, 5))
    record_merges(case, range(1, 6), store=second)
    assert_no_offer(briefing(case, capsys), "Four plus five merges on this machine are only nine")
    record_finish(case, 6, store=second)
    assert_offer(briefing(case, capsys), "Count ten local merges across repos, including overlapping issue numbers")
    assert STAR_PUT not in case.gh.calls


@pytest.mark.parametrize("choice", [
    pytest.param("yes", id="yes"),
    pytest.param("no", id="no"),
    pytest.param("", id="empty"),
    pytest.param("dismiss", id="dismissed"),
    pytest.param("true", id="non-yes"),
])
def test_star_prompt_terminal_answer_is_persisted_machine_wide(star_case, capsys, choice):
    case = star_case
    record_merges(case, range(1, 11))
    briefing(case, capsys)
    assert STAR_PUT not in case.gh.calls, "The initial screen must not star automatically"
    case.gh.calls.clear()
    assert_no_offer(answer(case, capsys, choice), "An answered question must disappear")
    if choice == "yes":
        assert case.gh.calls.count(STAR_PUT) == 1, "Explicit yes must PUT a star on Foragent exactly once"
        assert all(call in (["gh", "auth", "status"], STAR_PUT) for call in case.gh.calls)
    else:
        assert case.gh.calls == [], f"Non-yes outcome {choice!r} must be final without invoking gh"
    assert_machine_record(case)
    case.gh.calls.clear()
    assert_no_offer(briefing(case, capsys), f"The next fa brief after {choice!r} must not ask again")
    assert STAR_PUT not in case.gh.calls

    # Every invocation uses a new StateStore; also switch the active repo and
    # advance time and work so a per-repo flag or temporary cooldown cannot pass.
    switch_repository(case, "owner/second")
    case.clock[0] += timedelta(days=365)
    record_merges(case, range(1, 41))
    case.gh.calls.clear()
    for _ in range(2):
        assert_no_offer(briefing(case, capsys), f"{choice} must suppress all future requests on this machine")
    assert STAR_PUT not in case.gh.calls


def test_star_prompt_screen_close_without_an_answer_is_final(star_case, capsys):
    case = star_case
    record_merges(case, range(1, 11))
    assert_offer(briefing(case, capsys), "The first eligible human screen must ask once")
    # The CLI invocation has returned, closing this screen. Do not submit any
    # answer or synthetic close command: exposure itself must persist the state.
    assert_machine_record(case)
    assert STAR_PUT not in case.gh.calls, "Closing without an answer is not consent to star"
    case.gh.calls.clear()
    assert_no_offer(briefing(case, capsys), "The next fa brief after an unanswered screen closes must be silent")
    assert STAR_PUT not in case.gh.calls

    # New stores, another repository, time and more gate merges must not revive
    # the opportunity after the first screen was closed without an answer.
    switch_repository(case, "owner/second")
    case.clock[0] += timedelta(days=365)
    record_merges(case, range(1, 41))
    for _ in range(2):
        assert_no_offer(briefing(case, capsys), "An unanswered first screen is final on this machine")
    assert STAR_PUT not in case.gh.calls


@pytest.mark.parametrize("auth", ["logged-out", "missing"])
def test_star_prompt_requires_gh_auth_without_consuming_the_opportunity(star_case, capsys, auth):
    case = star_case
    record_merges(case, range(1, 11))
    case.gh.auth = auth
    assert_no_offer(briefing(case, capsys), "No request may be shown when gh cannot authenticate")
    assert STAR_PUT not in case.gh.calls
    case.gh.auth = "authenticated"
    assert_offer(briefing(case, capsys), "Restored gh authentication must reveal the still-unshown request")
    assert STAR_PUT not in case.gh.calls


@pytest.mark.parametrize("arguments", [
    pytest.param(("--star",), id="missing-value"),
    pytest.param(("--star", "yes", "--unexpected-option"), id="unknown-option"),
])
def test_star_prompt_invalid_answer_never_stars_or_consumes_the_question(star_case, capsys, arguments):
    case = star_case
    record_merges(case, range(1, 11))
    # Keep the existing test name, but distinguish malformed CLI syntax before
    # any screen opens from a human's empty, dismissed or other non-yes answer.
    code, _, _ = command(case, capsys, "brief", *arguments)
    assert code != 0, "Malformed CLI syntax must fail before opening a human screen"
    assert case.gh.calls == [], "Invalid input must be rejected before any gh call"
    assert not (case.gh.machine / "star-prompt.json").exists(), "A parser error did not expose the question"
    assert_offer(briefing(case, capsys), "Malformed CLI syntax must leave the first human question available")
    assert STAR_PUT not in case.gh.calls


def test_star_prompt_appears_only_in_the_human_briefing(star_case, capsys):
    from foragent import notify
    from foragent.issue import parse_body
    from foragent.run_prompts import build_prompt, build_resume_prompt, build_review_prompt
    from foragent.scheduler import tick

    case = star_case
    record_merges(case, range(1, 11))
    code, output, error = command(case, capsys, "brief", "--json")
    assert code == 0, error
    assert isinstance(json.loads(output), dict), "Keep the machine-readable briefing valid JSON"
    assert_no_offer(output, "JSON is not a human-facing screen")

    report = tick(tick_context(case, start_runs=False))
    assert_no_offer(json.dumps(report.actions), "The background gate must not ask for a star")
    for flags in ((), ("--json",)):
        code, output, error = command(case, capsys, "gate", "status", *flags)
        assert code == 0, error
        assert_no_offer(output, "Gate status is not the /fa briefing")

    config = replace(case.ctx.config, notify="claude-push")
    assert notify.send(config, "Build finished", "Finished issue #10")
    outbox = case.store.root / "push-outbox.jsonl"
    assert_no_offer(outbox.read_text(encoding="utf-8"), "Notifications must not carry a star request")
    body = case.tracker.get_issue(1).body
    parsed = parse_body(body)
    for prompt in (build_prompt(1, parsed, case.config.acceptance_dir),
                   build_review_prompt(parsed, "No changes"),
                   build_resume_prompt(1, body, body)):
        assert_no_offer(prompt, "Worker and reviewer prompts must not solicit a star")
    assert case.gh.calls == [], "Non-human surfaces must not even check gh for the star feature"

    assert_offer(briefing(case, capsys), "Only the human briefing must display the eligible request")
    assert STAR_PUT not in case.gh.calls
