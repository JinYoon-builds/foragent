"""Acceptance test for baseline-prebuild. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import fcntl
import json
import os
from pathlib import Path
import shlex
from threading import Event, Lock
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import git_command
from foragent import capacity, executors, runner, scheduler
from foragent.runner import run_issue
from test_baseline_cache import (
    another_repository, assert_merged, cache_case, prepare_history,
)
from test_orch_review_handoff import tick_context
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


# Observe the same subprocess boundary as baseline-cache; neither discovery,
# cache locking, slot acquisition nor run_issue itself is replaced. A prebuild
# uses an immutable base checkout independently of any issue's model executor.
# The existing orch event envelope records baseline-build with the full SHA in
# summary. Waiting is observable through waited_seconds and waiting_until;
# a particular thread class, polling interval or private build API is not required.


def eventually(predicate, message, *, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        Event().wait(0.01)


def build_events(case):
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    return [event for line in path.read_text().splitlines()
            if (event := json.loads(line))["kind"] == "baseline-build"]


class PrebuildProcesses:
    """Supply JUnit at the process boundary; execute tiny verification checks."""

    def __init__(self, case, monkeypatch):
        self.case = case
        self.cache = case.store.root.parent / "baselines"
        self.passing = {case.base_sha: set(case.passing_ids)}
        self.results = []
        self.builds, self.commands, self.errors = [], [], []
        self.lock = Lock()
        self.entered, self.release = Event(), Event()
        self.release.set()
        self.block_sha = case.base_sha
        original = runner.run_logged

        def process(argv, *, cwd, **kwargs):
            assert argv[:2] == ["/bin/sh", "-c"], argv
            words = shlex.split(argv[2])
            report = None
            for index, word in enumerate(words):
                if word.startswith(("--junitxml=", "--junit-xml=")):
                    report = Path(word.split("=", 1)[1])
                elif word in {"--junitxml", "--junit-xml"}:
                    report = Path(words[index + 1])
            with self.lock:
                self.commands.append((Path(cwd), words))
            if report is None:
                return original(argv, cwd=cwd, **kwargs)

            build = SimpleNamespace(sha=git_command(Path(cwd), "rev-parse", "HEAD"),
                                    cwd=Path(cwd), words=words,
                                    timeout=kwargs["timeout_s"], returned=Event())
            with self.lock:
                ordinal = len(self.builds)
                self.builds.append(build)
            try:
                assert build.cwd != case.store.worktree_path(case.number)
                assert case.config.acceptance_dir in words, words
                assert build.sha in self.passing, f"Unexpected baseline SHA: {build.sha}"
                if build.sha == self.block_sha:
                    self.entered.set()
                    assert self.release.wait(15), "Test did not release the baseline subprocess"
                result = self.results[ordinal] if ordinal < len(self.results) else "usable"
                if result == "no-junit":
                    return 0, False, None
                suite = ET.Element("testsuite")
                for node_id in sorted(self.passing[build.sha] | {case.future_id}):
                    path, name = node_id.split("::")
                    test = ET.SubElement(suite, "testcase", file=path, name=name,
                                         classname=path.removesuffix(".py").replace("/", "."))
                    if node_id == case.future_id:
                        ET.SubElement(test, "failure", message="another issue is not implemented")
                if result == "collection-error":
                    test = ET.SubElement(suite, "testcase", name="test_broken", classname="pytest")
                    ET.SubElement(test, "error", message="collection failure")
                report = report if report.is_absolute() else Path(cwd) / report
                report.parent.mkdir(parents=True, exist_ok=True)
                ET.ElementTree(suite).write(report, encoding="unicode")
                if result == "timeout":
                    # Even a parseable partial JUnit report must not be cached.
                    return None, True, None
                # Ordinary failures are usable: only the passing IDs are protected.
                return (2 if result == "collection-error" else 1), False, None
            except BaseException as exc:
                self.errors.append(exc)
                raise
            finally:
                build.returned.set()

        monkeypatch.setattr(runner, "run_logged", process)

    def idle(self):
        """Wait for #257's key locks, including cache publication, to finish."""
        if any(not build.returned.is_set() for build in self.builds):
            return False
        for path in self.cache.glob("*.lock"):
            with path.open("a") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True

    def wait_for_builds(self, count, message):
        eventually(lambda: len(self.builds) >= count or bool(self.errors), message)
        assert not self.errors, self.errors

    def wait_for_cache(self, count=1):
        eventually(lambda: len(list(self.cache.glob("*.json"))) == count and self.idle(),
                   f"Expected {count} completed machine-wide baseline cache entries")
        assert not self.errors, self.errors

    def assert_protected(self, case):
        prompt = case.implementer.calls[0]["prompt"]
        assert "already pass" in prompt
        assert all(node_id in prompt for node_id in case.passing_ids)
        assert case.future_id not in prompt
        verification = [words for cwd, words in self.commands
                        if cwd == case.store.worktree_path(case.number) and "pytest" in words]
        assert verification, "Cached passing IDs must still be verified on the implementation"
        for words in verification:
            assert {word for word in words if "::" in word} == case.passing_ids, words
            assert case.config.acceptance_dir not in words


@pytest.fixture
def prebuild(git_history, tmp_path, monkeypatch, isolated_home):
    """Use make_case's fake models and local Git; guard every OS signal."""
    def no_signal(*args, **kwargs):
        raise AssertionError(f"Acceptance tests must not signal processes: {args}")

    def fake_liveness(pid, signum):
        # tick may probe the in-process runner, but no real kill(2) is invoked.
        assert signum == 0, f"Unexpected process signal: {(pid, signum)}"

    def no_model(*args, **kwargs):
        raise AssertionError("Acceptance tests must not start real model turns")

    monkeypatch.setattr(os, "kill", fake_liveness)
    monkeypatch.setattr(os, "killpg", no_signal)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", no_model)
    monkeypatch.setattr(executors.CodexExecutor, "run", no_model)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    monkeypatch.setattr(capacity, "machine_root", lambda: isolated_home / ".local/share/foragent")
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)
    prepare_history(git_history)
    case = cache_case(git_history, tmp_path / "case", monkeypatch, number=1, test_slots=1)
    checks = PrebuildProcesses(case, monkeypatch)
    try:
        yield case, checks
    finally:
        checks.release.set()
        eventually(checks.idle, "A background baseline did not release its cache lock")
        assert not checks.errors, checks.errors


def tick_without_waiting(pool, case):
    # Running tick in a test thread lets a synchronous build fail promptly;
    # every caller releases the blocked subprocess before joining the pool.
    future = pool.submit(scheduler.tick, tick_context(case))
    report = future.result(timeout=5)
    assert report.skipped is None, report
    return report


def test_tick_prebuilds_once_in_background_and_records_sha(prebuild):
    case, checks = prebuild
    # No runnable issue is needed to warm a newly observed main commit.
    case.store.finish_run(case.number, outcome="test-setup")
    case.tracker.remove_labels(case.number, ["approved", "running"])
    checks.release.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            tick_without_waiting(pool, case)
            eventually(lambda: bool(build_events(case)),
                       f"tick did not record kind=baseline-build for main SHA {case.base_sha}")
            checks.wait_for_builds(1, "tick did not start baseline pytest in the background")
            assert case.base_sha in build_events(case)[0]["summary"]
            assert not list(checks.cache.glob("*.json")), "An unfinished build was cached"
            # Fresh TickContext instances must find the same in-progress key.
            tick_without_waiting(pool, case)
            assert len(checks.builds) == len(build_events(case)) == 1
        finally:
            checks.release.set()
    checks.wait_for_cache()
    assert checks.builds[0].timeout == 900
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick_without_waiting(pool, case)
    assert len(checks.builds) == len(build_events(case)) == 1, "A completed key was rebuilt"
    assert not case.implementer.calls and not case.reviewer.calls


def test_run_reuses_tick_prebuilt_baseline_from_another_repository_store(
    prebuild, tmp_path, monkeypatch,
):
    case, checks = prebuild
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick_without_waiting(pool, case)
    checks.wait_for_builds(1, "tick left a cold baseline for run_issue to build")
    checks.wait_for_cache()
    history = another_repository(case.history, tmp_path / "other-repository")
    consumer = cache_case(history, tmp_path / "consumer", monkeypatch,
                          number=2, repo="owner/other", test_slots=1)
    assert consumer.store.root != case.store.root
    assert consumer.base_sha == case.base_sha
    assert_merged(consumer, run_issue(consumer.number, consumer.ctx))
    assert len(checks.builds) == 1, "run_issue reran baseline pytest instead of consuming the prebuild"
    checks.assert_protected(consumer)


def test_run_waits_for_prebuild_without_spending_baseline_or_run_budget(prebuild, monkeypatch):
    case, checks = prebuild
    waiting = Event()
    update = case.store.update_run

    def observe(number, **fields):
        result = update(number, **fields)
        if fields.get("waiting_until") is not None:
            waiting.set()
        return result

    monkeypatch.setattr(case.store, "update_run", observe)
    checks.release.clear()
    started_at = case.clock[0]
    waited = case.config.max_runtime_minutes * 60 + 901
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            tick_without_waiting(pool, case)
            checks.wait_for_builds(1, "tick did not start the baseline that the runner must wait for")
            consumer = pool.submit(run_issue, case.number, case.ctx)
            assert waiting.wait(5), "Runner did not persist its wait for the in-progress prebuild"
            assert not consumer.done() and not case.implementer.calls
            case.clock[0] += timedelta(seconds=waited)
            assert len(checks.builds) == 1, "Waiting runner started its own baseline pytest"
        finally:
            checks.release.set()
        assert_merged(case, consumer.result(timeout=20))
    checks.wait_for_cache()
    run = case.store.get_run(case.number)
    assert run.waited_seconds == waited, "Prebuild waiting was charged to the run budget"
    assert run.executor_waited_seconds == 0
    assert run.waiting_until is None
    assert (case.clock[0] - started_at).total_seconds() == waited
    assert [build.timeout for build in checks.builds] == [900]
    assert case.implementer.calls[0]["timeout_s"] == case.config.max_runtime_minutes * 60
    assert case.store.load_flag(case.number) is None
    checks.assert_protected(case)


def test_prebuild_queues_for_one_machine_test_slot_and_releases_it(prebuild, monkeypatch):
    case, checks = prebuild
    queued, competitor_queued, competitor_started = Event(), Event(), Event()
    hold_slot = capacity.hold_test_slot

    @contextmanager
    def observed_slot(ceiling, *, waiting=None):
        def observe_wait():
            queued.set()
            if waiting is not None:
                waiting()

        with hold_slot(ceiling, waiting=observe_wait):
            yield

    def competitor():
        with hold_slot(1, waiting=competitor_queued.set):
            competitor_started.set()

    monkeypatch.setattr(capacity, "hold_test_slot", observed_slot)
    checks.release.clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            with hold_slot(1):
                tick_without_waiting(pool, case)
                assert queued.wait(5), "tick did not queue a prebuild for the occupied machine test slot"
                assert not checks.builds, "Baseline pytest bypassed an occupied test slot"
                tick_without_waiting(pool, case)
                assert len(build_events(case)) == 1, "A queued key was scheduled twice"
            checks.wait_for_builds(1, "Prebuild did not acquire the released test slot")
            other = pool.submit(competitor)
            assert competitor_queued.wait(5), "Prebuild did not hold the machine test slot during pytest"
            assert not competitor_started.is_set()
        finally:
            checks.release.set()
        other.result(timeout=10)
    checks.wait_for_cache()
    assert competitor_started.is_set(), "Prebuild retained the test slot after pytest finished"
    assert len(checks.builds) == 1


@pytest.mark.parametrize("unusable", ["timeout", "no-junit", "collection-error"])
def test_tick_retries_unusable_prebuild_once_per_key_per_tick(prebuild, unusable):
    case, checks = prebuild
    checks.results = [unusable, unusable, "usable"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        for attempt in (1, 2):
            tick_without_waiting(pool, case)
            checks.wait_for_builds(attempt, f"tick did not attempt {unusable} prebuild #{attempt}")
            eventually(checks.idle, "Unusable prebuild did not finish")
            # Let an accidental same-tick retry reach the observed boundary.
            Event().wait(0.1)
            assert len(checks.builds) == attempt, "A failed key retried more than once in one tick"
            assert len(build_events(case)) == attempt
            assert not list(checks.cache.glob("*.json")), f"The unusable {unusable} prebuild was cached"
        tick_without_waiting(pool, case)
        checks.wait_for_builds(3, "A later tick did not retry the failed prebuild")
        checks.wait_for_cache()
        tick_without_waiting(pool, case)
    assert len(checks.builds) == len(build_events(case)) == 3, "Recovered prebuild was not reused"
    assert all(case.base_sha in event["summary"] for event in build_events(case))
    assert_merged(case, run_issue(case.number, case.ctx))
    assert len(checks.builds) == 3, "Runner rebuilt a successfully retried prebuild"
    checks.assert_protected(case)


def test_main_move_prebuilds_new_sha_while_old_run_keeps_old_baseline(
    prebuild, tmp_path, monkeypatch,
):
    from dataclasses import replace

    case, checks = prebuild
    # Two test slots permit the new key to build while the old key is blocked.
    case.config = case.ctx.config = replace(case.config, test_slots=2)
    waiting = Event()
    update = case.store.update_run

    def observe(number, **fields):
        result = update(number, **fields)
        if fields.get("waiting_until") is not None:
            waiting.set()
        return result

    monkeypatch.setattr(case.store, "update_run", observe)
    checks.release.clear()
    new_id = "tests/acceptance/test_new_base.py::test_new_base"
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            tick_without_waiting(pool, case)
            checks.wait_for_builds(1, "tick did not prebuild the old main SHA")
            old_run = pool.submit(run_issue, case.number, case.ctx)
            assert waiting.wait(5), "Old-base runner did not wait for its baseline"
            case.history.update({"tests/acceptance/test_new_base.py": "def test_new_base():\n    assert True\n"})
            new_sha = case.history.tip
            assert new_sha != case.base_sha
            checks.passing[new_sha] = case.passing_ids | {new_id}
            tick_without_waiting(pool, case)
            checks.wait_for_builds(2, "Next tick did not build for the new main SHA")
            # The new cache must finish even while the old build remains blocked.
            eventually(lambda: len(list(checks.cache.glob("*.json"))) == 1,
                       "New-SHA baseline could not finish independently of the old build")
            assert {build.sha for build in checks.builds} == {case.base_sha, new_sha}
            assert any(new_sha in event["summary"] for event in build_events(case))
            assert not old_run.done() and not case.implementer.calls
        finally:
            checks.release.set()
        assert_merged(case, old_run.result(timeout=20))
    checks.wait_for_cache(2)
    checks.assert_protected(case)
    assert new_id not in case.implementer.calls[0]["prompt"], "Old run switched to the new main baseline"
    consumer = cache_case(case.history, tmp_path / "new-base-run", monkeypatch, number=2, test_slots=2)
    consumer.passing_ids.add(new_id)
    assert_merged(consumer, run_issue(consumer.number, consumer.ctx))
    checks.assert_protected(consumer)
    assert len(checks.builds) == 2, "A run rebuilt one of the SHA-specific prebuilds"
