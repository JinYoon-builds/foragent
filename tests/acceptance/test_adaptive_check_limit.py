"""Locked acceptance for adaptive-check-limit; exercise the public run entry point."""

from datetime import timedelta
from pathlib import Path
import re
import shlex
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import git_command
from foragent import runner
from foragent.reasons import Reason
from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


KEEP = "tests/acceptance/test_measured_selection.py"
NODES = (f"{KEEP}::test_alpha", f"{KEEP}::test_beta")


def measured_case(git_history, tmp_path, monkeypatch, *, measured=769, mode="healthy", timeout_exit=-9):
    """Keep the runner, capture, limits and retries real; fake processes and time."""
    git_history.update({KEEP: "def test_alpha(): pass\ndef test_beta(): pass\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[], reviewer="none",
        test_command="uv run pytest -q", max_runtime_minutes=360, max_retries=1,
    )
    # A broken runner may spend both worker attempts. Let it finish so the
    # assertions report that behaviour instead of an exhausted fake executor.
    case.implementer.steps = [case.implement] * (case.config.max_retries + 1)
    case.captures, case.protected = [], []
    case.measured = measured
    # Capture has an existing module-level clock dependency; verification uses
    # RunContext.monotonic. Drive both with the same fake clock, without replacing
    # either capture or check policy.
    monkeypatch.setattr("foragent.run_baseline.monotonic", case.ctx.monotonic)

    def process(argv, *, cwd, timeout_s, log_path, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        words = shlex.split(argv[2])
        report = next((word.split("=", 1)[1] for word in words
                       if word.startswith("--junitxml=")), None)
        selected = tuple(word for word in words if word.startswith("tests/"))
        if report is not None and "--collect-only" not in words:
            # The capture and protected check contain the same two passing
            # tests. No slower, unrelated suite supplies the measurement.
            assert selected == ("tests/acceptance/",), selected
            assert measured < timeout_s, (measured, timeout_s)
            case.clock[0] += timedelta(seconds=measured)
            suite = ET.Element("testsuite", tests="2", time=str(measured))
            for node in NODES:
                file, name = node.split("::")
                ET.SubElement(suite, "testcase", file=file, name=name,
                              classname=file[:-3].replace("/", "."), time=str(measured / 2))
            ET.ElementTree(suite).write(report, encoding="unicode")
            case.captures.append(selected)
            return 0, False, ""
        if set(NODES).intersection(selected):
            assert set(selected) == set(NODES) and len(selected) == len(NODES), selected
            assert Path(cwd) == case.store.worktree_path(1), cwd
            index = len(case.protected)
            if mode == "timeout" or mode == "recover" and index == 0:
                # Include process shutdown time so elapsed and allowance differ,
                # without requiring a particular decimal formatting policy.
                elapsed, timed_out, code = timeout_s + 7, True, timeout_exit
            elif mode == "assertion":
                elapsed, timed_out, code = 1, False, 1
            else:
                duration = measured * 1.5
                elapsed, timed_out = min(duration, timeout_s + 0.25), duration > timeout_s
                code = -9 if timed_out else 0
            case.clock[0] += timedelta(seconds=elapsed)
            case.protected.append(SimpleNamespace(
                command=argv[2], limit=timeout_s, elapsed=elapsed, timed_out=timed_out,
                workers=len(case.implementer.calls), attempts=case.store.get_run(1).attempts,
            ))
            # Do not put timing diagnostics into the fake's output: the real
            # gate must produce those from its measurement and supplied limit.
            with Path(log_path).open("a") as log:
                log.write("protected assertion failed\n" if mode == "assertion" else "protected process\n")
            return code, timed_out, ""
        return 0, False, ""

    monkeypatch.setattr(runner, "run_logged", process)
    return case


def evidence(case):
    return case.store.log_path(1).read_text()


@pytest.mark.parametrize("measured", [640, 769, 880])
def test_healthy_protected_check_uses_twice_its_measurement(
    git_history, tmp_path, monkeypatch, measured,
):
    case = measured_case(git_history, tmp_path, monkeypatch, measured=measured)

    outcome = run_issue(1, case.ctx)

    assert outcome == "merged", (
        f"A healthy protected selection taking {measured * 1.5:g}s (>900s) "
        f"must pass within twice its {measured}s measurement; outcome={outcome}; "
        f"limits={[check.limit for check in case.protected]}\n{evidence(case)}"
    )
    assert case.captures == [("tests/acceptance/",)]
    assert len(case.protected) == 1, "Run every protected test exactly once on success"
    assert case.protected[0].limit == 2 * measured
    assert len(case.implementer.calls) == case.store.get_run(1).attempts == 1


def test_gate_retries_a_check_timeout_once_with_more_time_and_no_worker_retry(
    git_history, tmp_path, monkeypatch,
):
    case = measured_case(git_history, tmp_path, monkeypatch, mode="recover")

    outcome = run_issue(1, case.ctx)

    assert outcome == "merged", evidence(case)
    observed = [(check.workers, check.attempts) for check in case.protected]
    assert observed == [(1, 1), (1, 1)], (
        f"The gate must retry the check once on the same implementation; observed={observed}"
    )
    first, retry = case.protected
    assert first.limit == 2 * case.measured
    assert first.timed_out and not retry.timed_out
    assert retry.limit > first.limit, "A check retry must receive a larger limit"
    assert len(case.implementer.calls) == case.store.get_run(1).attempts == 1
    assert case.captures == [("tests/acceptance/",)]
    text = evidence(case)
    assert re.search(r"timeout|timed\s+out", text, re.I), text
    numbers = {float(value) for value in re.findall(r"\d+(?:\.\d+)?", text)}
    assert {first.elapsed, first.limit} <= numbers, "Report timing even when the check retry recovers"


@pytest.mark.parametrize("timeout_exit", [-9, 0])
def test_exhausted_check_timeout_reports_times_without_charging_implementation(
    git_history, tmp_path, monkeypatch, timeout_exit,
):
    case = measured_case(git_history, tmp_path, monkeypatch, mode="timeout", timeout_exit=timeout_exit)

    outcome = run_issue(1, case.ctx)

    run = case.store.get_run(1)
    observed = (outcome, run.outcome, run.status, len(case.implementer.calls), run.attempts)
    assert observed == ("timeout", "timeout", "finished", 1, 1), (
        "A protected-check timeout must stay a timeout and preserve the implementation "
        f"retry budget; observed={observed}; limits={[c.limit for c in case.protected]}"
    )
    assert len(case.protected) == 2, "Retry a timed-out check once, then stop"
    assert case.captures == [("tests/acceptance/",)]
    first, retry = case.protected
    assert first.limit == 2 * case.measured and retry.limit > first.limit
    assert all(check.timed_out and check.workers == check.attempts == 1 for check in case.protected)
    assert case.store.load_flag(1).reason == Reason.TIMEOUT
    assert case.tracker.merged_prs == []
    detail = run.detail
    assert re.search(r"timeout|timed\s+out", detail, re.I), detail
    assert any(word in detail.lower() for word in ("protected", "acceptance", "baseline")), detail
    numbers = {float(value) for value in re.findall(r"\d+(?:\.\d+)?", detail)}
    assert {retry.elapsed, retry.limit} <= numbers, (
        f"Check timeout must report measured {retry.elapsed:g}s and allowed {retry.limit:g}s; "
        f"detail={detail!r}"
    )
    assert "Checks or review failed after all attempts" not in evidence(case)


def test_an_actual_protected_assertion_failure_still_uses_implementation_retries(
    git_history, tmp_path, monkeypatch,
):
    case = measured_case(git_history, tmp_path, monkeypatch, mode="assertion")

    outcome = run_issue(1, case.ctx)

    assert outcome == "retries-exceeded", evidence(case)
    assert len(case.implementer.calls) == case.store.get_run(1).attempts == 2
    assert len(case.protected) == 2 and not any(check.timed_out for check in case.protected)
    assert "protected assertion failed" in case.implementer.calls[1]["prompt"]
    assert case.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert case.tracker.merged_prs == []
