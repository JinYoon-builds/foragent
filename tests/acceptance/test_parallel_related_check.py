"""Acceptance test for parallel-related-check.

With the group setting at its default (2), a related check that selects two test
files runs them in two pytest processes at the same time, each holding its own
machine test slot, and records the same per-file and total facts as a serial check.

Processes are fake (runner.run_logged); slots are the real capacity.hold_test_slot
in the isolated machine root, observed by a counting wrapper. No signal is sent.
"""

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import shlex
import threading
import xml.etree.ElementTree as ET

from conftest import ACCEPTANCE
from foragent.runner import run_issue
from test_related_tests_record import (
    FILE_SECONDS,
    SELECTED_FILES,
    isolated_home,
    offline_acceptance,
    related_record_case,
    related_record_safety,
    stored_related_checks,
)

# Both group processes share one wall-clock span of this length.
WALL_SECONDS = 9.0
CASES = {
    SELECTED_FILES[0]: (("TestValue", "test_value[a]", "3.5"), ("TestValue", "test_value[b]", "2.75")),
    SELECTED_FILES[1]: (("", "test_first", "1.5"), ("", "test_second", "0.25")),
}

__all__ = ["isolated_home", "offline_acceptance", "related_record_safety"]


def _report_path(words, cwd):
    for index, word in enumerate(words):
        if word.split("=", 1)[0] in {"--junitxml", "--junit-xml"}:
            report = Path(word.split("=", 1)[1] if "=" in word else words[index + 1])
            return report if report.is_absolute() else Path(cwd) / report
    return None


def _write_report(report, files, failing):
    report.parent.mkdir(parents=True, exist_ok=True)
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite", time=str(WALL_SECONDS))
    for path in files:
        module = path.removesuffix(".py").replace("/", ".")
        for cls, name, seconds in CASES[path]:
            testcase = ET.SubElement(suite, "testcase", classname=".".join(filter(None, (module, cls))),
                                     name=name, time=seconds, file=path)
            if path in failing:
                ET.SubElement(testcase, "failure", message="offline test failure")
    ET.ElementTree(suites).write(report, encoding="unicode")


def parallel_case(git_history, tmp_path, monkeypatch, *, failing_rounds=()):
    """The locked related-record world, with a fake pytest that proves concurrency.

    ``failing_rounds`` maps a related-check round index to the set of files whose
    tests fail in that round.
    """
    from foragent import capacity, runner

    rounds = max([0, *[index + 1 for index, _ in failing_rounds]]) + 1
    case = related_record_case(git_history, tmp_path, monkeypatch, checks=[{}] * rounds)
    failing = dict(failing_rounds)
    case.related_calls = []
    case.lock = threading.Lock()
    case.active_slots = 0
    case.max_slots = 0
    started = threading.Barrier(2, timeout=10)
    finished = threading.Barrier(2, timeout=10)

    real_hold = capacity.hold_test_slot

    @contextmanager
    def counted_hold(*args, **kwargs):
        with real_hold(*args, **kwargs):
            with case.lock:
                case.active_slots += 1
                case.max_slots = max(case.max_slots, case.active_slots)
            try:
                yield
            finally:
                with case.lock:
                    case.active_slots -= 1

    monkeypatch.setattr(capacity, "hold_test_slot", counted_hold)

    def check_process(argv, *, cwd, timeout_s, log_path, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        words = shlex.split(argv[2])
        report = _report_path(words, cwd)
        selected = [word for word in words if word in SELECTED_FILES]
        if not selected:
            if report is not None:
                suite = ET.Element("testsuite")
                ET.SubElement(suite, "testcase", file=ACCEPTANCE, name="test_feature",
                              classname=ACCEPTANCE.removesuffix(".py").replace("/", "."), time="99")
                report.parent.mkdir(parents=True, exist_ok=True)
                ET.ElementTree(suite).write(report, encoding="unicode")
            case.clock[0] += timedelta(seconds=2)
            return 0, False, ""

        with case.lock:
            round_index = len(case.related_calls) // 2
            call = {"selected_files": selected, "report": report, "concurrent": False, "slots": 0,
                    "round": round_index}
            case.related_calls.append(call)
        bad = failing.get(round_index, set())
        if len(selected) == 1:
            try:
                # Both group processes must be inside their command at once.
                if started.wait() == 0:
                    case.clock[0] += timedelta(seconds=WALL_SECONDS)
                with case.lock:
                    call["slots"] = case.active_slots
                finished.wait()
                call["concurrent"] = True
            except threading.BrokenBarrierError:
                started.reset()
                finished.reset()
                case.clock[0] += timedelta(seconds=WALL_SECONDS)
        else:
            case.clock[0] += timedelta(seconds=WALL_SECONDS)
        if report is not None:
            _write_report(report, selected, bad)
        return (1 if set(selected) & bad else 0), False, ""

    monkeypatch.setattr(runner, "run_logged", check_process)
    return case


def assert_parallel_round(case, round_index):
    calls = [call for call in case.related_calls if call["round"] == round_index]
    assert sorted(tuple(call["selected_files"]) for call in calls) == sorted((path,) for path in SELECTED_FILES), (
        "Two selected files with the group setting at 2 must run as two pytest processes, one file each: "
        f"{[call['selected_files'] for call in case.related_calls]}")
    assert all(call["concurrent"] for call in calls), "The two group processes must run at the same time"
    assert all(call["slots"] >= 2 for call in calls), (
        f"Each group process must hold its own test slot while running: {[call['slots'] for call in calls]}")
    assert calls[0]["report"] is not None and calls[1]["report"] is not None, "Each group needs a JUnit report"


def test_two_selected_files_run_in_two_concurrent_pytest_processes_with_serial_facts(
        git_history, tmp_path, monkeypatch):
    case = parallel_case(git_history, tmp_path, monkeypatch)

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert_parallel_round(case, 0)
    assert len(case.related_calls) == 2
    assert stored_related_checks(case) == [
        {"changed_file_count": 3, "selected_files": list(SELECTED_FILES),
         "file_seconds": FILE_SECONDS, "total_seconds": WALL_SECONDS, "timed_out": False},
    ], "One check record: per-file seconds merged from both reports, total = the check's wall time"


def test_a_failing_group_fails_the_whole_related_check_like_a_serial_check(git_history, tmp_path, monkeypatch):
    case = parallel_case(git_history, tmp_path, monkeypatch, failing_rounds=[(0, {SELECTED_FILES[1]})])

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert_parallel_round(case, 0)
    assert_parallel_round(case, 1)
    assert len(case.related_calls) == 4
    assert len(case.implementer.calls) == 2, "A failing group must fail the related check and spend a worker retry"
    assert stored_related_checks(case) == [
        {"changed_file_count": 3, "selected_files": list(SELECTED_FILES),
         "file_seconds": FILE_SECONDS, "total_seconds": WALL_SECONDS, "timed_out": False},
        {"changed_file_count": 4, "selected_files": list(SELECTED_FILES),
         "file_seconds": FILE_SECONDS, "total_seconds": WALL_SECONDS, "timed_out": False},
    ]
