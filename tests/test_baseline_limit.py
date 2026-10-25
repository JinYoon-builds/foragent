"""Capture budgets follow successful repository history without weakening the gate."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shlex
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import BASE_TIME
from foragent import run_baseline, runner
from foragent.config import Config
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure
from foragent.run_checks import CheckResult
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from runner_support import run_case


NODE = "tests/acceptance/test_keep.py::test_keep"


@pytest.fixture
def capture_case(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    now = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    c = SimpleNamespace(now=now, clock=0.0, duration=736.0, calls=[], base="base",
                        timed_out=False, exit_code=0, cache=tmp_path / "baselines")

    def add_worktree(root, sha):
        path = root / NODE.split("::", 1)[0]
        path.parent.mkdir(parents=True)
        path.write_text("def test_keep(): pass\n")

    git = SimpleNamespace(ref_sha=lambda ref: c.base, blob_sha=lambda *args: None,
                          add_detached_worktree=add_worktree, remove_worktree=lambda root: None,
                          at=lambda root: SimpleNamespace(head_sha=lambda: c.base, _call=lambda args: ""))
    c.ctx = runner.RunContext(
        repo="owner/repo", repo_root=repo, tracker=FakeTracker(), store=store, git=git,
        config=Config(test_command="pytest", max_runtime_minutes=24 * 60), now=lambda: now[0],
    )

    def check(command, root, log, timeout_s, **kwargs):
        c.calls.append(timeout_s)
        c.clock += c.duration
        report = Path(next(word.split("=", 1)[1] for word in shlex.split(command)
                           if word.startswith("--junitxml=")))
        suite = ET.Element("testsuite", time=str(c.duration))
        case = ET.SubElement(suite, "testcase", file=NODE.split("::", 1)[0], name="test_keep")
        if c.exit_code == 1:
            ET.SubElement(case, "failure")
        ET.ElementTree(suite).write(report, encoding="unicode")
        return CheckResult(command, c.exit_code, c.timed_out, "")

    monkeypatch.setattr(runner, "_check", check)
    monkeypatch.setattr(run_baseline, "monotonic", lambda: c.clock)

    def capture(api):
        if api == "runner":
            c.log = store.log_path(1)
            return run_baseline._capture_acceptance_baseline(1, c.ctx, "pytest", "main")
        build = run_baseline._baseline_build(c.ctx, "pytest", "main", store.log_path(1))
        c.log = c.cache / f"{build.key}.log"
        lock = StateStore(c.cache).lock_acceptance_baseline(build.key)
        run_baseline._build_baseline(c.ctx, build, lock)

    c.capture = capture
    return c


def seed_history(c, duration, *, name="history", repository=None, age=timedelta(days=1)):
    saved = {
        "node_ids": [NODE], "collected_ids": [NODE], "failing_ids": [], "excluded": [],
        "repository": str(c.ctx.repo_root.resolve()) if repository is None else repository,
        "duration_seconds": duration, "created_at": (c.now[0] - age).isoformat(),
    }
    c.cache.mkdir(exist_ok=True)
    path = c.cache / f"{name}.json"
    path.write_text(json.dumps(saved))
    return path


def seed_xml_history(c, duration, *, wrapped=True, **kwargs):
    path = seed_history(c, duration, **kwargs)
    saved = json.loads(path.read_text())
    del saved["duration_seconds"]
    if "repository" not in kwargs:
        del saved["repository"]
    path.write_text(json.dumps(saved))
    root = ET.Element("testsuites" if wrapped else "testsuite")
    suite = ET.SubElement(root, "testsuite") if wrapped else root
    suite.set("time", str(duration))
    ET.ElementTree(root).write(path.with_suffix(".xml"), encoding="unicode")
    return path


@pytest.mark.parametrize("api", ["prebuild", "runner"])
@pytest.mark.parametrize("duration,expected", [
    (None, 900), (736, 1472), (2000, 3600), (100, 900), (736.1, 1472),
])
def test_baseline_capture_limit_uses_history_floor_cap_and_default(capture_case, api, duration, expected):
    c = capture_case
    if duration is not None:
        seed_history(c, duration)
    c.capture(api)
    assert c.calls == [expected]
    log = c.log.read_text()
    start = next(line for line in log.splitlines() if line.startswith("Acceptance baseline capture starting:"))
    assert f"limit={expected}s" in start
    assert ("default: no successful capture history" if duration is None
            else f"last successful capture={duration:g}s") in start


@pytest.mark.parametrize("api", ["prebuild", "runner"])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("duration,expected", [(736, 1472), (736.069, 1472), (2000, 3600), (100, 900)])
def test_baseline_capture_limit_reads_legacy_xml_history(capture_case, api, wrapped, duration, expected):
    c = capture_case
    seed_xml_history(c, duration, wrapped=wrapped)
    c.capture(api)
    assert c.calls == [expected]
    assert f"last successful capture={duration:g}s" in c.log.read_text()


@pytest.mark.parametrize("api", ["prebuild", "runner"])
def test_baseline_refresh_uses_its_own_expired_legacy_xml(capture_case, api):
    c = capture_case
    build = run_baseline._baseline_build(c.ctx, "pytest", "main", c.ctx.store.log_path(1))
    seed_xml_history(c, 736, name=build.key)
    c.capture(api)
    assert c.calls == [1472]
    saved = json.loads((c.cache / f"{build.key}.json").read_text())
    assert saved["duration_seconds"] == 736


@pytest.mark.parametrize("unusable", ["timeout", "no-junit"])
def test_baseline_failed_refresh_preserves_successful_xml_history(capture_case, monkeypatch, unusable):
    c = capture_case
    build = run_baseline._baseline_build(c.ctx, "pytest", "main", c.ctx.store.log_path(1))
    path = seed_xml_history(c, 736, name=build.key)
    saved_json, saved_xml = path.read_bytes(), path.with_suffix(".xml").read_bytes()
    c.duration, c.timed_out, c.exit_code = 2000, True, 143
    check = runner._check

    def no_junit(command, root, log, timeout_s, **kwargs):
        c.calls.append(timeout_s)
        return CheckResult(command, 0, False, "")

    if unusable == "no-junit":
        monkeypatch.setattr(runner, "_check", no_junit)
    c.capture("prebuild")
    assert "acceptance baseline unavailable" in c.log.read_text()
    assert path.read_bytes() == saved_json
    assert path.with_suffix(".xml").read_bytes() == saved_xml
    c.duration, c.timed_out, c.exit_code = 700, False, 0
    monkeypatch.setattr(runner, "_check", check)
    c.capture("prebuild")
    assert c.calls == [1472, 1472]


@pytest.mark.parametrize("xml_is_newest", [False, True])
def test_baseline_limit_uses_newest_success_across_xml_and_recorded_history(capture_case, xml_is_newest):
    c = capture_case
    older, newer = (seed_history, seed_xml_history) if xml_is_newest else (seed_xml_history, seed_history)
    older(c, 2000, name="older", age=timedelta(days=2))
    newer(c, 736, name="newer")
    seed_xml_history(c, 2000, name="other-repo", repository="/other/repository", age=timedelta())
    c.capture("prebuild")
    assert c.calls == [1472]


def test_baseline_limit_prefers_recorded_duration_over_xml(capture_case):
    c = capture_case
    path = seed_xml_history(c, 2000, repository=str(c.ctx.repo_root.resolve()))
    saved = json.loads(path.read_text())
    saved["duration_seconds"] = 736
    path.write_text(json.dumps(saved))
    c.capture("prebuild")
    assert c.calls == [1472]


@pytest.mark.parametrize("xml", [
    "<testsuite", "<unrelated time='736'/>", "<testsuites/>", "<testsuite/>",
    "<testsuite time='invalid'/>", "<testsuite time='-1'/>", "<testsuite time='nan'/>",
])
def test_baseline_limit_ignores_unusable_legacy_xml(capture_case, xml):
    c = capture_case
    path = seed_xml_history(c, 736)
    path.with_suffix(".xml").write_text(xml)
    c.capture("prebuild")
    assert c.calls == [900]


@pytest.mark.parametrize("remaining,expected", [(500, 500), (1600, 1472)])
def test_baseline_run_limit_is_bounded_by_remaining_budget(capture_case, monkeypatch, remaining, expected):
    c = capture_case
    seed_history(c, 736)
    monkeypatch.setattr(runner, "_remaining", lambda *args: remaining)
    c.capture("runner")
    assert c.calls == [expected]
    assert f"run remaining={remaining}s" in c.log.read_text()


@pytest.mark.parametrize("first_api,next_api", [
    ("prebuild", "prebuild"), ("runner", "runner"),
    ("prebuild", "runner"), ("runner", "prebuild"),
])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_successful_baseline_records_duration_for_next_build(capture_case, first_api, next_api, exit_code):
    c = capture_case
    c.exit_code = exit_code
    c.capture(first_api)
    entry, = c.cache.glob("*.json")
    saved = json.loads(entry.read_text())
    assert saved["duration_seconds"] == 736
    assert saved["repository"] == str(c.ctx.repo_root.resolve())
    c.base = "next-base"
    c.now[0] += timedelta(hours=7)
    c.capture(next_api)
    assert c.calls == [900, 1472]


def test_baseline_limit_uses_latest_repository_success_across_cache_keys(capture_case):
    c = capture_case
    seed_history(c, 2000, name="older", age=timedelta(days=2))
    seed_history(c, 736, name="newer")
    seed_history(c, 2000, name="other-repo", repository="/other/repository", age=timedelta())
    c.capture("prebuild")
    assert c.calls == [1472]


@pytest.mark.parametrize("duration", [None, "736", -1, True, float("nan"), float("inf")])
def test_baseline_limit_ignores_unusable_history(capture_case, duration):
    c = capture_case
    seed_history(c, duration)
    (c.cache / "malformed.json").write_text("{")
    (c.cache / "legacy.json").write_text(json.dumps({"node_ids": [NODE], "created_at": BASE_TIME.isoformat()}))
    c.capture("prebuild")
    assert c.calls == [900]


@pytest.mark.parametrize("api", ["prebuild", "runner"])
def test_baseline_timeout_does_not_cache_success_or_duration(capture_case, api):
    c = capture_case
    c.timed_out, c.exit_code = True, 143
    if api == "runner":
        with pytest.raises(AttemptFailure, match="acceptance baseline unavailable"):
            c.capture(api)
    else:
        c.capture(api)
        assert "acceptance baseline unavailable" in c.log.read_text()
    assert not list(c.cache.glob("*.json"))
    c.timed_out, c.exit_code = False, 0
    c.capture(api)
    assert c.calls == [900, 900]


def test_baseline_timeout_still_refuses_merge_without_protection(run_case, monkeypatch):
    c = run_case
    c.history.update({NODE.split("::", 1)[0]: "def test_keep(): pass\n"})
    c.ctx.config = replace(c.ctx.config, test_command="pytest", max_runtime_minutes=120)
    calls = []

    def timeout(command, worktree, log, timeout_s, **kwargs):
        calls.append(timeout_s)
        return CheckResult(command, 143, True, "timed out")

    monkeypatch.setattr(runner, "_check", timeout)
    ticks = iter((0.0, 900.0))
    monkeypatch.setattr(run_baseline, "monotonic", lambda: next(ticks))
    assert runner.run_issue(1, c.ctx) == "acceptance-baseline-unavailable"
    assert calls == [900]
    assert c.executor.calls == []
    assert c.tracker.created_prs == c.tracker.merged_prs == []
    assert c.store.load_flag(1).reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert not list((c.store.root.parent / "baselines").glob("*.json"))
