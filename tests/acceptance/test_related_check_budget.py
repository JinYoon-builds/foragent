"""Acceptance test for related-check-budget. Locked by Foragent; do not edit."""

from datetime import timedelta
from pathlib import Path
import re
import shlex
import xml.etree.ElementTree as ET

from conftest import ACCEPTANCE, git_command
from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


TEST_COMMAND = "uv run pytest -q"
RELATED_TESTS = (
    "tests/test_budget_alpha.py",
    "tests/test_budget_beta.py",
    "tests/unit/test_budget_gamma.py",
)


def budget_case(git_history, tmp_path, monkeypatch, *, remaining, check_times_out=False):
    """Real runner, budget clock and test selection; fake only external processes."""
    from foragent import runner

    git_history.update({
        "src/sample/__init__.py": "",
        "src/sample/budget.py": "VALUE = 1\n",
        **{path: (
            "from sample.budget import VALUE\n\n"
            "def test_value():\n    assert VALUE > 0\n"
        ) for path in RELATED_TESTS},
    })
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[],
        test_command=TEST_COMMAND, max_runtime_minutes=90,
    )
    case.check_commands = []

    def implement(kwargs):
        case.implement(kwargs)
        (kwargs["worktree"] / "src/sample/budget.py").write_text("VALUE = 2\n")
        if len(case.implementer.calls) == 1:
            # Spend part of the 90-minute run in the worker, so the check must
            # use the current remainder rather than the configured total.
            case.clock[0] += timedelta(seconds=case.config.max_runtime_minutes * 60 - remaining)

    # Let the buggy retry path complete normally instead of failing because the
    # fake worker ran out of steps. A check timeout must not use these retries.
    case.implementer.steps = [implement] * (case.config.max_retries + 1)

    def check_process(argv, *, cwd, timeout_s, log_path, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        command = argv[2]
        words = shlex.split(command)
        case.check_commands.append((command, Path(cwd), timeout_s))
        report = next((arg.split("=", 1)[1] for arg in words
                       if arg.startswith("--junitxml=")), None)
        if report is not None:
            suite = ET.Element("testsuite")
            ET.SubElement(suite, "testcase", file=ACCEPTANCE, name="test_feature",
                          classname=ACCEPTANCE.removesuffix(".py").replace("/", "."))
            ET.ElementTree(suite).write(report, encoding="unicode")
        elif check_times_out and set(RELATED_TESTS).intersection(words):
            # Model the process actually consuming its supplied limit. No real
            # sleeping, failing assertion or test-failure output is involved.
            case.clock[0] += timedelta(seconds=timeout_s)
            return -9, True, ""
        return 0, False, ""

    # _check -> runner.run_logged(timeout_s=...) -> communicate(timeout=...).
    # Keep _check real and observe the same boundary as test_fast_checks.py.
    monkeypatch.setattr(runner, "run_logged", check_process)
    return case


def related_checks(case):
    checks = [(command, timeout) for command, cwd, timeout in case.check_commands
              if cwd == case.store.worktree_path(1)
              and set(RELATED_TESTS).intersection(shlex.split(command))]
    assert checks, f"No related-unit check reached the subprocess boundary: {case.check_commands}"
    for command, _ in checks:
        selected = {word for word in shlex.split(command) if word.startswith("tests/")}
        assert selected == set(RELATED_TESTS), f"Unexpected related-test selection: {command}"
    return checks


def assert_check_timeout(case, outcome, checks):
    run = case.store.get_run(1)
    command, limit = checks[0]
    detail = run.detail
    text = detail.casefold()
    # Accept the command or a descriptive check name, without prescribing the
    # sentence format. Each selected file contains exactly one test.
    observed = {
        "outcome": outcome,
        "stored_outcome": run.outcome,
        "status": run.status,
        "worker_calls": len(case.implementer.calls),
        "attempts": run.attempts,
        "related_check_calls": len(checks),
        "detail_names_check": command in detail or all(
            word in text for word in ("related", "unit", "test")
        ),
        "detail_names_timeout": bool(re.search(r"\b(?:timeout|timed\s+out)\b", text)),
        "detail_names_limit_seconds": bool(re.search(rf"\b{limit}\b", detail)),
        "detail_names_selected_count": bool(re.search(rf"\b{len(RELATED_TESTS)}\b", detail)),
    }
    assert observed == {
        "outcome": "timeout",
        "stored_outcome": "timeout",
        "status": "finished",
        "worker_calls": 1,
        "attempts": 1,
        "related_check_calls": 1,
        "detail_names_check": True,
        "detail_names_timeout": True,
        "detail_names_limit_seconds": True,
        "detail_names_selected_count": True,
    }, (
        "A related-unit check hitting its limit must end the run as a check timeout, "
        "without spending an implementation retry. "
        f"Observed: {observed}; detail={detail!r}; check_limit={limit}s; "
        f"selected_tests={len(RELATED_TESTS)}"
    )


def test_related_check_uses_remaining_hour_instead_of_fixed_900_seconds(
    git_history, tmp_path, monkeypatch,
):
    case = budget_case(git_history, tmp_path, monkeypatch, remaining=60 * 60)

    outcome = run_issue(1, case.ctx)

    assert outcome == "merged", case.store.log_path(1).read_text()
    checks = related_checks(case)
    assert len(checks) == 1, checks
    command, timeout = checks[0]
    assert timeout == 3600, (
        f"Related-unit check must receive the run's remaining 3600 seconds, "
        f"not a fixed 900-second limit; received timeout_s={timeout}: {command}"
    )


def test_related_check_timeout_ends_run_with_detail_without_worker_retry(
    git_history, tmp_path, monkeypatch,
):
    case = budget_case(
        git_history, tmp_path, monkeypatch, remaining=60 * 60, check_times_out=True,
    )

    outcome = run_issue(1, case.ctx)

    assert_check_timeout(case, outcome, related_checks(case))


def test_related_check_below_floor_gets_900_seconds_and_reports_check_timeout(
    git_history, tmp_path, monkeypatch,
):
    case = budget_case(
        git_history, tmp_path, monkeypatch, remaining=5 * 60, check_times_out=True,
    )

    outcome = run_issue(1, case.ctx)

    checks = related_checks(case)
    assert checks[0][1] == 900, (
        f"With 300 seconds remaining, the related-unit check must still receive "
        f"the 900-second floor, not zero or the smaller remainder: {checks}"
    )
    assert_check_timeout(case, outcome, checks)
