"""Real measurement watchdogs and cancellation recovery without treating CI as red."""

from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from time import monotonic

import pytest
import yaml

from foragent import ci_mode, ci_pytest, ci_shard, merge_owner_batch as batch
from foragent.ci_workflow import TEST_HANG_SECONDS, workflow_text
from foragent.config import Config
from foragent.tracker import GhTracker
import test_merge_owner_batch as batch_tests
from test_merge_owner_batch import batched, proto, world  # noqa: F401 -- shared fixture dependencies
import test_merge_owner_proto as support


@pytest.fixture
def measurement_session(tmp_path):
    shutil.copyfile(Path(ci_pytest.__file__), tmp_path / "fa_measurement.py")

    def run(source, seconds=None, *, conftest=None):
        (tmp_path / "test_subject.py").write_text(source)
        if conftest is not None:
            (tmp_path / "conftest.py").write_text(conftest)
        report = tmp_path / "measurement.json"
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"FA_TEST_HANG_SECONDS", "FA_CACHED_RESULTS", "FA_SHARD_TIMES",
                                      "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}}
        environment.update(PYTHONPATH=str(tmp_path), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                           FA_SHARD="0", FA_SHARDS="1", FA_REPORT=str(report))
        if seconds is not None:
            environment["FA_TEST_HANG_SECONDS"] = seconds
        started = monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "fa_measurement"],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15,
        )
        return result, monotonic() - started, report

    return run


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_hung_test_exits_with_traceback_and_no_complete_measurement(measurement_session, phase):
    source = (
        "import time\nimport pytest\n"
        "@pytest.fixture\ndef subject():\n"
        f"    {'time.sleep(30)' if phase == 'setup' else 'pass'}\n"
        "    yield\n"
        f"    {'time.sleep(30)' if phase == 'teardown' else 'pass'}\n"
        "def test_sleeping_subject(subject):\n"
        f"    {'time.sleep(30)' if phase == 'call' else 'pass'}\n"
    )
    result, elapsed, report = measurement_session(source, "2")
    assert result.returncode != 0 and elapsed < 15
    assert "Timeout" in result.stderr and "test_subject.py" in result.stderr
    assert ("test_sleeping_subject" if phase == "call" else "in subject") in result.stderr
    assert not report.exists(), "A process killed mid-protocol cannot publish a complete measurement"


@pytest.mark.parametrize("seconds", [None, "", "invalid", "0", "-1", "1.5"])
def test_absent_or_invalid_hang_setting_keeps_old_callers_working(measurement_session, seconds):
    result, _, report = measurement_session(
        "import time\ndef test_slow_but_allowed():\n    time.sleep(3)\n", seconds,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(report.read_text())["complete"] is True


def test_watchdog_restarts_per_test_and_is_cancelled_after_protocol(measurement_session):
    source = "import time\n" + "\n".join(
        f"def test_progress_{index}():\n    time.sleep(1)\n" for index in range(3)
    )
    result, elapsed, report = measurement_session(
        source, "2", conftest="import time\ndef pytest_sessionfinish(session):\n    time.sleep(3)\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed > 2
    assert len(json.loads(report.read_text())["passing_ids"]) == 3


def test_measure_forwards_hang_setting_and_rejects_aborted_measurement(tmp_path, monkeypatch, capfd):
    root = tmp_path / "repo"
    acceptance = root / "tests/acceptance"
    acceptance.mkdir(parents=True)
    (acceptance / "test_hung.py").write_text("import time\ndef test_hung():\n    time.sleep(30)\n")
    shutil.copyfile(Path(ci_pytest.__file__), tmp_path / "fa_measurement.py")
    monkeypatch.setenv("FA_TEST_HANG_SECONDS", "2")
    monkeypatch.setenv("FA_SHARD", "0")
    monkeypatch.setenv("FA_SHARDS", "1")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    started = monotonic()
    measured = ci_shard.measure(root, f"{shlex.quote(sys.executable)} -m pytest -p no:cacheprovider",
                                "tests/acceptance", [], tmp_path, "head")
    assert monotonic() - started < 15
    assert measured["complete"] is False
    assert "test_hung" in capfd.readouterr().err


@pytest.mark.parametrize("job,step", [("acceptance", "compare"), ("acceptance-base", "produce")])
def test_both_measurement_steps_enable_the_watchdog(job, step):
    document = yaml.safe_load(workflow_text(Config()))
    measure = next(item for item in document["jobs"][job]["steps"] if item.get("id") == step)
    assert measure["env"]["FA_TEST_HANG_SECONDS"] == str(TEST_HANG_SECONDS)
    assert TEST_HANG_SECONDS > 0


@pytest.mark.parametrize("buckets,status,cancelled", [
    (["cancel"], "fail", True),
    (["pass", "cancel"], "fail", True),
    (["pending", "cancel"], "fail", True),
    (["fail", "cancel"], "fail", False),
    (["fail"], "fail", False),
    (["pending"], "pending", False),
    (["pass", "skipping"], "pass", False),
    ([], "none", False),
])
def test_cancelled_only_reader_preserves_legacy_checks_results(buckets, status, cancelled):
    def github(args, input=None):
        assert args[:3] == ["pr", "checks", "7"]
        output = json.dumps([{"bucket": bucket} for bucket in buckets])
        if status in {"fail", "pending"}:
            raise subprocess.CalledProcessError(1 if status == "fail" else 8, ["gh", *args],
                                                output=output, stderr="")
        return output

    tracker = GhTracker("owner/repo", run=github)
    assert tracker.pr_checks(7) == status
    assert tracker.pr_checks_cancelled_only(7) is cancelled


def test_rerun_cancelled_targets_only_completed_cancelled_or_timed_out_head_runs():
    calls = []
    runs = [{"id": index, "status": status, "conclusion": conclusion} for index, (status, conclusion) in enumerate([
        ("completed", "cancelled"), ("completed", "timed_out"), ("completed", "failure"),
        ("completed", "success"), ("in_progress", "cancelled"), ("queued", None),
    ])]

    def github(args, input=None):
        calls.append(args)
        if args == ["api", "repos/owner/repo/pulls/7"]:
            return json.dumps({"head": {"sha": "current-head"}})
        if args[-1] == "repos/owner/repo/actions/runs?head_sha=current-head&per_page=100":
            return json.dumps(runs)
        assert args[:2] == ["run", "rerun"]
        return ""

    GhTracker("owner/repo", run=github).rerun_cancelled(7)
    assert calls[2:] == [["run", "rerun", str(index), "--failed", "-R", "owner/repo"] for index in (0, 1)]


@pytest.mark.parametrize("cancelled_only", [True, False], ids=["cancelled", "real-failure"])
def test_batch_reruns_cancelled_tip_and_still_retires_real_failure(batched, cancelled_only):
    case = batched
    case.pending_batch = True
    batch_tests.queue_workers(case, [5, 1])
    support.gate(case)
    tip, = batch_tests.groups(case)
    case.tracker.set_pr_checks(tip.number, "fail", cancelled_only=cancelled_only)
    assert case.tracker.pr_checks(tip.number) == "fail"
    support.gate(case)
    assert not case.target_pushes and not case.merges
    if cancelled_only:
        assert batch_tests.refs(case) and not batch.retirements(case.ctx) and not batch_tests.events(case)
        assert case.tracker.rerun_cancelled_prs == [tip.number]
        assert len(case.batch_pushes) == 1
        batch_tests.green(case)
        support.gate(case)
        assert len(case.target_pushes) == 1 and not batch_tests.refs(case)
    else:
        assert not batch_tests.refs(case) and not case.tracker.rerun_cancelled_prs
        assert batch_tests.events(case)[-1]["reason"] == "tip CI red"


def test_cancelled_tip_keeps_the_existing_silence_stop_rule(batched):
    case = batched
    case.pending_batch = True
    batch_tests.queue_workers(case, [5, 1])
    support.gate(case)
    tip, = batch_tests.groups(case)
    base, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    proof = batch.Evidence(base, chain, tip.number, case.clock[0].timestamp())
    case.tracker.set_pr_checks(tip.number, "fail", cancelled_only=True)
    with pytest.raises(ci_mode.MissingComparison, match="prefix CI cancelled; re-run requested"):
        proof.reports(case.ctx)
    assert proof.pending and case.tracker.rerun_cancelled_prs == [tip.number]
    path = case.store.root / "merge-batch" / f"{chain[-1][2]}.json"
    progress = json.loads(path.read_text())
    case.clock[0] = datetime.fromtimestamp(progress["last_progress"] + batch.SILENCE_SECONDS,
                                           tz=case.clock[0].tzinfo)
    with pytest.raises(ValueError, match="silence"):
        proof.reports(case.ctx)
    assert json.loads(path.read_text())["last_progress"] == progress["last_progress"]
