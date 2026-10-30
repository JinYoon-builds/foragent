"""Acceptance test for related-tests-record. Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import timedelta
import os
from pathlib import Path
import shlex
import subprocess
import xml.etree.ElementTree as ET

import pytest

from conftest import ACCEPTANCE, git_command
from foragent.runner import run_issue
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


SELECTED_FILES = ("tests/test_selection.py", "tests/unit/test_selection_importer.py")
FILE_SECONDS = {SELECTED_FILES[0]: 6.25, SELECTED_FILES[1]: 1.75}


@pytest.fixture(autouse=True)
def related_record_safety(tmp_path, monkeypatch, isolated_home, offline_acceptance):
    """Only offline fixture Git may spawn; checks, model turns and signals are fake."""
    from foragent import executors

    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Related-test recording must not signal a process or start a real model turn")

    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)


def related_record_case(git_history, tmp_path, monkeypatch, *, checks=(), no_selection=False):
    """Observe Run.related_tests through a fresh StateStore, including read_history.

    The smallest public record is a list on Run, persisted in runs/<issue>.json
    and the existing runs.jsonl completion history. Each check has
    changed_file_count (all diff paths supplied to selection), selected_files
    (sorted repo-relative paths), file_seconds (summed JUnit testcase times),
    total_seconds (elapsed check time, including pytest overhead), and timed_out.
    Retries append checks; no check is an empty list. Missing timeout reports
    use an empty file_seconds map, without inventing zero-duration measurements.
    """
    from foragent import runner

    git_history.update({
        "src/sample/__init__.py": "",
        "src/sample/selection.py": "VALUE = 1\n",
        "src/sample/extra.py": "EXTRA = 1\n",
        "docs/context.txt": "before\n",
        SELECTED_FILES[0]: (
            "import pytest\nfrom sample.selection import VALUE\n\nclass TestValue:\n"
            "    @pytest.mark.parametrize('variant', ['a', 'b'])\n"
            "    def test_value(self, variant):\n        assert VALUE > 0\n"
        ),
        SELECTED_FILES[1]: (
            "from sample.selection import VALUE\n\ndef test_first():\n    assert VALUE > 0\n"
            "\ndef test_second():\n    assert VALUE > 0\n"
        ),
        "tests/test_unrelated.py": "def test_other():\n    assert True\n",
    })
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[],
                     test_command="uv run pytest -q", runner_unit_tests="all", max_runtime_minutes=15)
    case.related_calls = []

    def implement(kwargs):
        case.implement(kwargs)
        if not no_selection:
            (kwargs["worktree"] / "src/sample/selection.py").write_text("VALUE = 2\n")
            (kwargs["worktree"] / "docs/context.txt").write_text("after\n")
            if len(case.implementer.calls) > 1:
                (kwargs["worktree"] / "src/sample/extra.py").write_text("EXTRA = 2\n")

    case.implementer.steps = [implement] * (case.config.max_retries + 1)

    def check_process(argv, *, cwd, timeout_s, log_path, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        words = shlex.split(argv[2])
        report = None
        for index, word in enumerate(words):
            if word.split("=", 1)[0] in {"--junitxml", "--junit-xml"}:
                report = Path(word.split("=", 1)[1] if "=" in word else words[index + 1])
                if not report.is_absolute():
                    report = Path(cwd) / report
        selected = [word for word in words if word in SELECTED_FILES]
        if not selected:
            # Acceptance baseline and Done-when checks are distinct from the
            # related check, even though they can also produce JUnit reports.
            if report is not None:
                suite = ET.Element("testsuite")
                ET.SubElement(suite, "testcase", file=ACCEPTANCE, name="test_feature",
                              classname=ACCEPTANCE.removesuffix(".py").replace("/", "."), time="99")
                report.parent.mkdir(parents=True, exist_ok=True)
                ET.ElementTree(suite).write(report, encoding="unicode")
            case.clock[0] += timedelta(seconds=2)
            return 0, False, ""

        index = len(case.related_calls)
        assert index < len(checks), f"Unexpected related check: {argv[2]}"
        plan = checks[index]
        assert selected == list(SELECTED_FILES), argv[2]
        if plan["timed_out"]:
            assert plan["seconds"] == timeout_s, "A fake timeout must consume the supplied check limit"
        case.related_calls.append({"selected_files": selected, "report": report})
        if report is not None and plan["report"] != "missing":
            report.parent.mkdir(parents=True, exist_ok=True)
            suites = ET.Element("testsuites")
            suite = ET.SubElement(suites, "testsuite", time=str(plan["seconds"]))
            times = ((SELECTED_FILES[0], "TestValue", "test_value[a]", "3.5"),
                     (SELECTED_FILES[0], "TestValue", "test_value[b]", "2.75"),
                     (SELECTED_FILES[1], "", "test_first", "1.5"),
                     (SELECTED_FILES[1], "", "test_second", "0.25"))
            if plan["report"] == "partial":
                times = times[:1]
            for path, cls, name, seconds in times:
                module = path.removesuffix(".py").replace("/", ".")
                attributes = {"classname": ".".join(filter(None, (module, cls))),
                              "name": name, "time": seconds}
                # Honor the runner's chosen pytest family. Default xunit2
                # omits file; legacy reports supply the exact relative path.
                if "junit_family=legacy" in words or "junit_family=xunit1" in words:
                    attributes["file"] = path
                testcase = ET.SubElement(suite, "testcase", **attributes)
                if plan["exit_code"]:
                    ET.SubElement(testcase, "failure", message="offline test failure")
            ET.ElementTree(suites).write(report, encoding="unicode")
        case.clock[0] += timedelta(seconds=plan["seconds"])
        return plan["exit_code"], plan["timed_out"], ""

    monkeypatch.setattr(runner, "run_logged", check_process)
    return case


def stored_related_checks(case):
    """Read only public records after run_issue has removed its temporary worktree."""
    reader = StateStore(case.store.root)
    current = asdict(reader.get_run(1))
    assert "related_tests" in current, "Run.related_tests must persist related-unit selection and timings"
    finished, = reader.read_history()
    assert asdict(finished)["related_tests"] == current["related_tests"], (
        "Completion history must retain checks for facts after a later execution replaces the current run")
    return current["related_tests"]


def check_plan(*, seconds=13.25, exit_code=0, timed_out=False, report="complete"):
    return {"seconds": seconds, "exit_code": exit_code, "timed_out": timed_out, "report": report}


def test_related_tests_record_sums_junit_case_seconds_by_selected_file(git_history, tmp_path, monkeypatch):
    case = related_record_case(git_history, tmp_path, monkeypatch, checks=[check_plan()])

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(case.related_calls) == 1
    record, = stored_related_checks(case)
    assert record == {"changed_file_count": 3, "selected_files": list(SELECTED_FILES),
                      "file_seconds": FILE_SECONDS, "total_seconds": 13.25, "timed_out": False}, (
        "Count feature, source and docs paths; sum parametrized/class testcase times, not suite time or wall time")
    assert case.related_calls[0]["report"] is not None, "Related checks need their own JUnit report"


def test_related_tests_record_keeps_each_check_across_worker_retries(git_history, tmp_path, monkeypatch):
    case = related_record_case(git_history, tmp_path, monkeypatch, checks=[
        check_plan(exit_code=1), check_plan(seconds=11.5),
    ])

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == len(case.related_calls) == 2
    assert stored_related_checks(case) == [
        {"changed_file_count": 3, "selected_files": list(SELECTED_FILES),
         "file_seconds": FILE_SECONDS, "total_seconds": 13.25, "timed_out": False},
        {"changed_file_count": 4, "selected_files": list(SELECTED_FILES),
         "file_seconds": FILE_SECONDS, "total_seconds": 11.5, "timed_out": False},
    ], "Append each failed/passing check with its own selection inputs and elapsed time"


@pytest.mark.parametrize("report", ["partial", "missing"])
def test_related_tests_record_timeout_retains_selection_and_total(git_history, tmp_path, monkeypatch, report):
    case = related_record_case(git_history, tmp_path, monkeypatch, checks=[
        check_plan(seconds=900, exit_code=-9, timed_out=True, report=report),
    ])

    # #435 (2026-10-30): this fake reports timed_out with no watchdog decision, which keeps main's timeout path;
    # a watchdog-stopped related check (stalled-restart) is covered by #435's own tests.
    assert run_issue(1, case.ctx) == "timeout", case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == len(case.related_calls) == 1
    record, = stored_related_checks(case)
    assert record == {"changed_file_count": 3, "selected_files": list(SELECTED_FILES),
                      "file_seconds": {SELECTED_FILES[0]: 3.5} if report == "partial" else {},
                      "total_seconds": 900, "timed_out": True}, (
        "Persist selection and measured total before the stop ends the run; keep only available file timings")


def test_related_tests_record_no_selected_unit_files_is_empty(git_history, tmp_path, monkeypatch):
    case = related_record_case(git_history, tmp_path, monkeypatch, no_selection=True)

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert case.related_calls == []
    assert stored_related_checks(case) == [], "No selected unit files is an empty check list, not a missing record"


def test_related_tests_record_timeout_does_not_reuse_prior_junit_times(git_history, tmp_path, monkeypatch):
    case = related_record_case(git_history, tmp_path, monkeypatch, checks=[
        check_plan(exit_code=1), check_plan(seconds=900, exit_code=-9, timed_out=True, report="missing"),
    ])

    # #435 (2026-10-30): this fake reports timed_out with no watchdog decision, which keeps main's timeout path;
    # a watchdog-stopped related check (stalled-restart) is covered by #435's own tests.
    assert run_issue(1, case.ctx) == "timeout", case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == len(case.related_calls) == 2
    assert stored_related_checks(case) == [
        {"changed_file_count": 3, "selected_files": list(SELECTED_FILES),
         "file_seconds": FILE_SECONDS, "total_seconds": 13.25, "timed_out": False},
        {"changed_file_count": 4, "selected_files": list(SELECTED_FILES),
         "file_seconds": {}, "total_seconds": 900, "timed_out": True},
    ], "A missing timeout report must not reuse the previous check's JUnit file"
