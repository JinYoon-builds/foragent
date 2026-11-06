"""Acceptance for closed-promises-stay-green (D122).

A locked acceptance test of a closed issue is a promise kept once. When the
base side of the CI acceptance reports that comparisons already read shows
such a test failing on main, the orchestrator gets one request naming the test
and the issue; the /fa report (`fa brief --report`) keeps naming it until a
later main CI shows it passing, and then one orch-events record of kind
``promise-cleared`` names the test and the issue. No test run is started for
this. A test owned by an open issue is not reported.

The world is the locked CI-mode world (tests/acceptance/test_ci_mode.py): real
runner, real GhTracker reads of recorded Actions artifacts, offline Git. Its
base side already has ``test_future`` failing on main. Here that file is a
locked test of issue #7, which merged and is closed.
"""

from datetime import timedelta
import json
import re
import subprocess

import pytest
import yaml

from conftest import git_command
from test_ci_mode import FUTURE, KEEP, mode_case, run
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401  (fixture)
from test_state_split import offline_acceptance  # noqa: F401  (fixture)

CLOSED = 7
OPEN = 8
FUTURE_FILE = FUTURE.split("::")[0]
OPEN_OWNED = "tests/acceptance/test_open_owned.py::test_open_owned"
OPEN_FILE = OPEN_OWNED.split("::")[0]
DISPATCH = re.compile(r"\b(workflow\s+run|run\s+rerun|rerun|dispatches)\b")


@pytest.fixture(autouse=True)
def quiet_world(monkeypatch, offline_acceptance):  # noqa: F811
    import os
    import signal

    from foragent import executors, scheduler

    def forbidden(*args, **kwargs):
        pytest.fail("promise checks must not signal processes or start model turns")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")


def _merged_history(case, number):
    """A finished, merged run of ``number``: the gate's own merge record."""
    store = case.store
    saved = case.clock[0]
    case.clock[0] = saved - timedelta(minutes=40 + number)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    case.clock[0] = saved - timedelta(minutes=30 + number)
    store.record_merge(number, case.clock[0])
    store.finish_run(number, outcome="merged")
    case.clock[0] = saved


def promise_case(git_history, tmp_path, monkeypatch, *, base_passing=(), open_owner=False,
                 at=None):
    """The CI-mode world plus closed issue #7 whose locked test is ``test_future``.

    ``base_passing`` changes only the base side of shard 0, the CI results
    main already has (the head contains main, so it passes there too).
    ``open_owner`` adds #8, merged but still open, owning
    tests/acceptance/test_open_owned.py, which fails on main.
    ``at`` starts this world's run later than an earlier world on the same store.
    """
    from foragent.tracker import Issue

    if open_owner:
        git_history.update({OPEN_FILE: "def test_open_owned():\n    assert False, 'broken on main'\n"})
    case = mode_case(git_history, tmp_path, monkeypatch)
    if at is not None:
        # A reused store: time moves on, and run #1 starts again at that time.
        case.clock[0] = at
        case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    case.tracker.add_issue(CLOSED, "Shipped feature", "Shipped feature", labels=[], state="closed")
    case.store.save_approval(CLOSED, body="Shipped feature", body_sha="shipped",
                             acceptance_blobs=case.git.blob_shas("origin/main", [FUTURE_FILE]),
                             approved_by="human", approved_at=(case.clock[0] - timedelta(hours=2)).isoformat())
    owners = [CLOSED]
    issues = [Issue(1, "Implement feature", "", frozenset({"approved", "running"}), "open"),
              Issue(9, "Queued feature", "", frozenset({"approved"}), "open"),
              Issue(CLOSED, "Shipped feature", "", frozenset(), "closed")]
    if open_owner:
        case.tracker.add_issue(OPEN, "Reopened feature", "Reopened feature", labels=[], state="open")
        case.store.save_approval(OPEN, body="Reopened feature", body_sha="reopened",
                                 acceptance_blobs=case.git.blob_shas("origin/main", [OPEN_FILE]),
                                 approved_by="human",
                                 approved_at=(case.clock[0] - timedelta(hours=2)).isoformat())
        owners.append(OPEN)
        issues.append(Issue(OPEN, "Reopened feature", "", frozenset(), "open"))
    if not any(run.number == CLOSED and case.store.run_merged(run) for run in case.store.read_history()):
        for number in owners:
            _merged_history(case, number)
    case.store.refresh_local_issues(issues, [], {}, case.clock[0])

    payload = case.ci.payload

    def with_base(index):
        value = payload(index)
        if index == 0:
            base = value["base"]
            extra = [OPEN_OWNED] if open_owner else []
            base["collected_ids"] = [*base["collected_ids"], *extra]
            passing = list(base["passing_ids"])
            passing += [node for node in base_passing if node not in passing]
            base["passing_ids"] = passing
            head = value["head"]
            head["collected_ids"] = [*head["collected_ids"], *extra]
            # The head contains main, so a test main passes again passes there too.
            head["passing_ids"] = [*head["passing_ids"],
                                   *(node for node in base_passing if node not in head["passing_ids"])]
        return value

    case.ci.payload = with_base
    gh_args = []
    # Every gh call the recorded endpoints do not answer reaches the legacy
    # recording: a workflow dispatch or rerun would arrive here.
    command = case.ci.legacy.command

    def recording(args, input=None):
        gh_args.append(list(args))
        return command(args, input)

    case.ci.legacy.command = recording
    case.gh_args = gh_args
    return case


def later_main(git_history):
    """Main moves on; the old issue branch is gone, as after a squash merge."""
    for root in (git_history.origin, git_history.clone):
        git_command(root, "update-ref", "-d", "refs/heads/foragent/issue-1")
    git_command(git_history.clone, "update-ref", "-d", "refs/remotes/origin/foragent/issue-1")


def requests(case):
    from foragent import orch_inbox

    return orch_inbox.requests(case.store)


def _text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _names_issue(record, number):
    text = _text(record)
    if re.search(rf"#{number}(?!\d)", text):
        return True
    return any(value == number for key, value in record.items() if "issue" in key.lower())


def promise_reports(case, test=FUTURE, number=CLOSED):
    return [record for record in requests(case) if test in _text(record) and _names_issue(record, number)]


def events(case, kind=None):
    path = case.store.root / "orch-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    return [row for row in rows if kind is None or row.get("kind") == kind]


def cleared(case, test=FUTURE, number=CLOSED):
    return [row for row in events(case, "promise-cleared") if test in _text(row) and _names_issue(row, number)]


def fa_report(case, tmp_path, monkeypatch, capsys):
    """`fa brief --report` for this world's state folder, local state only."""
    from foragent import cli, update_check
    from foragent.state import StateStore

    project = tmp_path / "report-project"
    project.mkdir(exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)
    (project / ".foragent.yml").write_text(yaml.safe_dump(
        {"repo": "owner/repo", "language": "en", "skill_libraries": []}), encoding="utf-8")
    root, clock = case.store.root, case.clock
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: cls(root, lambda: clock[0])))
    monkeypatch.setattr(cli.star, "offer", lambda language: None)
    monkeypatch.setattr(update_check, "cached", lambda config: None)
    monkeypatch.chdir(project)
    with monkeypatch.context() as local:
        def forbidden(*args, **kwargs):
            pytest.fail("The report reads local state only")

        local.setattr(subprocess, "Popen", forbidden)
        capsys.readouterr()
        code = cli_exit(cli.main, ["brief", "--report", "--repo-root", str(project)])
        out = capsys.readouterr().out
    # Each report records its time; reading it again must still show what stays open.
    (root / "report.json").unlink(missing_ok=True)
    assert code == 0, out
    return out


def shows_promise(output, test=FUTURE, number=CLOSED):
    name = test.rsplit("::", 1)[-1]
    return any(name in line and re.search(rf"#{number}(?!\d)", line) for line in output.splitlines())


def assert_no_new_test_run(case):
    assert not case.processes.captures, "the promise check must not capture a local baseline"
    executed = case.processes.head_ids
    assert FUTURE not in executed and not any(node.startswith(FUTURE_FILE) for node in executed), (
        "the promise check must read CI's base results, not run the closed issue's test")
    dispatched = [args for args in case.gh_args if DISPATCH.search(" ".join(args))]
    assert not dispatched, f"the promise check must not start a CI run: {dispatched}"


def test_closed_issues_failing_locked_test_on_main_reaches_the_orchestrator_once(
    git_history, tmp_path, monkeypatch,
):
    case = promise_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "merged"
    reports = promise_reports(case)
    assert len(reports) == 1, (
        f"one orchestrator request must name {FUTURE} and #{CLOSED}; requests: {_text(requests(case))}")
    assert reports[0].get("status") != "done"
    assert not promise_reports(case, KEEP), "a test passing on main is not a broken promise"
    assert_no_new_test_run(case)


def test_repeated_readings_of_the_same_failure_do_not_report_again(git_history, tmp_path, monkeypatch):
    # One run reads the comparison twice (regression, then repair); a later
    # run on a later main reads it again. The failure stays one report.
    case = promise_case(git_history, tmp_path, monkeypatch)
    case.ci.regression = "nonpassing"

    def repair(kwargs):
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 3\n")
        case.ci.repaired = True
        return "Repaired the reported acceptance regression"

    case.implementer.steps.append(repair)
    case.reviewer.steps.append("VERDICT: PASS")
    assert run(case) == "merged"
    assert len({head for head, _ in case.ci.reads}) >= 2, "the run must read two comparisons"
    assert len(promise_reports(case)) == 1
    later_main(git_history)
    again = promise_case(git_history, tmp_path, monkeypatch, at=case.clock[0] + timedelta(hours=1))
    assert again.base_sha != case.base_sha
    assert run(again) == "merged"
    assert len(promise_reports(again)) == 1, "a still-failing promise is reported once, not per reading"
    assert cleared(again) == []
    assert_no_new_test_run(again)


def test_failing_promise_stays_in_the_report_until_main_passes_then_clears_once(
    git_history, tmp_path, monkeypatch, capsys, isolated_home,  # noqa: F811
):
    case = promise_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "merged"
    assert len(promise_reports(case)) == 1
    first = fa_report(case, tmp_path, monkeypatch, capsys)
    assert shows_promise(first), f"the /fa report must name the broken promise:\n{first}"
    case.clock[0] += timedelta(hours=3)
    assert shows_promise(fa_report(case, tmp_path, monkeypatch, capsys)), "it stays visible while it fails"
    assert cleared(case) == []

    later_main(git_history)
    passing = promise_case(git_history, tmp_path, monkeypatch, base_passing=[FUTURE],
                           at=case.clock[0] + timedelta(hours=1))
    passing.ci.regression = "nonpassing"

    def repair(kwargs):
        (kwargs["worktree"] / "src/demo.py").write_text("VALUE = 3\n")
        passing.ci.repaired = True
        return "Repaired the reported acceptance regression"

    passing.implementer.steps.append(repair)
    passing.reviewer.steps.append("VERDICT: PASS")
    assert run(passing) == "merged"
    assert len({head for head, _ in passing.ci.reads}) >= 2, "main passes in two later readings"
    records = cleared(passing)
    assert len(records) == 1, (
        f"one promise-cleared record must name {FUTURE} and #{CLOSED}; events: {_text(events(passing))}")
    assert len(promise_reports(passing)) == 1, "clearing is not a second report"
    after = fa_report(passing, tmp_path, monkeypatch, capsys)
    assert not shows_promise(after), f"a kept promise leaves the /fa report:\n{after}"
    assert_no_new_test_run(passing)


def test_test_owned_by_an_open_issue_is_not_a_broken_promise(git_history, tmp_path, monkeypatch):
    case = promise_case(git_history, tmp_path, monkeypatch, open_owner=True)
    assert run(case) == "merged"
    assert len(promise_reports(case)) == 1, "the closed issue's failing test is still reported"
    wrong = [record for record in requests(case)
             if OPEN_OWNED in _text(record) or "test_open_owned" in _text(record) or _names_issue(record, OPEN)]
    assert wrong == [], f"a test of open issue #{OPEN} is not a broken promise: {_text(wrong)}"
    assert not [row for row in events(case) if "test_open_owned" in _text(row) and "promise" in _text(row)]
