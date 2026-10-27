"""Acceptance test for facts-baseline-trend. Locked by Foragent; do not edit."""

from copy import copy
from datetime import timedelta
from hashlib import sha256
import json
import os
import re
import shutil
import subprocess

import pytest

from foragent.state import StateStore
from test_baseline_cache import TEST_COMMAND, isolated_home
from test_baseline_prebuild import prebuild
from test_orch_facts import NOW, facts, facts_case
from test_state_split import offline_acceptance


# Wire contract: baseline_builds[] = {key, base_sha, created_at, test_count,
# duration_seconds, succeeded, timed_out}, newest created_at first.
# baseline_trend = {latest_duration_seconds, capture_limit_seconds, ratio,
# near_limit}. test_count counts protected passing node_ids. A usable capture
# can exit 1 because other acceptance tests have not been implemented yet.
# Cache metadata adds repo/base_sha/exit_code/timed_out to the existing payload;
# the key is the JSON filename stem. Missing legacy durations become null and
# are excluded from numerical comparisons. No capture-limit formula is frozen:
# observe the real background builder against a copy of the same history.


@pytest.fixture
def baseline_facts(prebuild, tmp_path, monkeypatch, capsys):
    producer, checks = prebuild
    producer.clock[0] = NOW
    case = facts_case(tmp_path, monkeypatch)
    case.store = producer.store
    case.root = producer.root
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: en\nskill_libraries: []\nnotify: none\n"
        f"test_command: {json.dumps(TEST_COMMAND)}\ntest_slots: 1\n",
        encoding="utf-8",
    )
    case.cache = checks.cache

    def seed(ordinal, duration, *, repo="owner/repo", timed_out=False, at=None, count=2):
        # Opposite filename/mtime ordering catches readers sorting by either
        # instead of the recorded build time. Historical SHAs need not be HEAD.
        base_sha = f"{ordinal:040x}"
        key = sha256(f"{repo}:{base_sha}:{TEST_COMMAND}".encode()).hexdigest()
        created_at = (at or NOW - timedelta(minutes=60 - ordinal)).isoformat()
        passing = sorted(producer.passing_ids)[:count] if not timed_out else []
        payload = {
            "repo": repo, "base_sha": base_sha, "created_at": created_at,
            "node_ids": passing, "collected_ids": [*passing, producer.future_id],
            "failing_ids": [producer.future_id], "excluded": [],
            "exit_code": None if timed_out else 1, "timed_out": timed_out,
        }
        if duration is not None:
            payload["duration_seconds"] = duration
        case.cache.mkdir(parents=True, exist_ok=True)
        path = case.cache / f"{key}.json"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        mtime = (NOW - timedelta(minutes=ordinal)).timestamp()
        os.utime(path, (mtime, mtime))
        return {
            "key": key, "base_sha": base_sha, "created_at": created_at,
            "test_count": len(passing), "duration_seconds": duration,
            "succeeded": not timed_out, "timed_out": timed_out,
        }

    def capture_limit():
        from foragent.run_baseline import _baseline_build, _build_baseline

        # A fake JUnit subprocess supplied by #257/#325 observes the timeout
        # passed by the actual builder. A separate cache prevents this probe
        # (including a future recorded failure) from becoming dashboard input.
        context = copy(producer.ctx)
        context.store = StateStore(tmp_path / "limit-probe" / "owner__repo", now=context.now)
        probe_cache = context.store.root.parent / "baselines"
        if case.cache.exists():
            shutil.copytree(case.cache, probe_cache)
        else:
            probe_cache.mkdir(parents=True)
        build = _baseline_build(context, TEST_COMMAND, producer.base_sha,
                                context.store.log_path(producer.number))
        checks.results.append("timeout")
        lock = StateStore(probe_cache).lock_acceptance_baseline(build.key)
        _build_baseline(context, build, lock)
        assert len(checks.builds) == 1, "Observe one real capture invocation, without any model turn"
        limit = checks.builds[0].timeout
        assert type(limit) in (int, float) and limit > 0
        return limit

    def read(*, human=False):
        def forbidden(*args, **kwargs):
            pytest.fail("Facts must only read records, never launch or signal a process")

        # Git fixture setup/probing finishes before the original facts guards
        # apply. facts() also checks every local file's bytes/mtime and gh calls.
        with monkeypatch.context() as observation:
            observation.setattr(subprocess, "Popen", forbidden)
            observation.setattr(os, "kill", forbidden)
            observation.setattr(os, "killpg", forbidden)
            return facts(case, capsys, human=human)

    case.seed = seed
    case.capture_limit = capture_limit
    case.read = read
    return case


def test_facts_baseline_trend_lists_repository_cache_history_newest_first(baseline_facts):
    case = baseline_facts
    oldest = case.seed(1, 195, at=NOW - timedelta(days=2), count=1)
    newest = case.seed(3, 900, timed_out=True)
    middle = case.seed(2, 485)
    # A machine-wide cache contains other repositories, even at the same SHA.
    case.seed(3, 9999, repo="owner/other", at=NOW - timedelta(seconds=1))

    rows = case.read()["baseline_builds"]
    assert len(rows) == 3, "Read this repository's builds, including history older than the cache reuse TTL"
    expected = [newest, middle, oldest]
    assert [{field: row[field] for field in expected[0]} for row in rows] == expected, (
        "Use cache keys and recorded times, retain timeouts, and count passing baseline IDs rather than all collected tests")


def test_facts_baseline_trend_uses_latest_success_and_the_real_runner_limit(baseline_facts):
    case = baseline_facts
    case.seed(1, 850)
    case.seed(2, 500)
    case.seed(3, 400)
    case.seed(4, 2000, timed_out=True)
    case.seed(5, 9000, repo="owner/other")
    limit = case.capture_limit()

    trend = case.read()["baseline_trend"]
    assert trend["latest_duration_seconds"] == 400, "A newer timeout or another repository must not replace the latest success"
    assert trend["capture_limit_seconds"] == limit, "Use the runner's current policy, including #427 when available"
    assert trend["ratio"] == pytest.approx(400 / limit)
    assert trend["near_limit"] is (400 / limit >= 0.75)


@pytest.mark.parametrize("duration", [674, 675, 676])
def test_facts_baseline_trend_compares_duration_to_three_quarters_of_limit(baseline_facts, duration):
    case = baseline_facts
    case.seed(1, duration)
    limit = case.capture_limit()

    trend = case.read()["baseline_trend"]
    assert trend["latest_duration_seconds"] == duration
    assert trend["capture_limit_seconds"] == limit
    assert trend["ratio"] == pytest.approx(duration / limit)
    # These straddle the original 900s boundary, without imposing that limit on
    # #427's successor policy. Equality must count as near the limit.
    assert trend["near_limit"] is (duration >= 0.75 * limit)


@pytest.mark.parametrize(("durations", "rising"), [
    ((10, 20, 30), True),
    ((10, 20), False),
    ((10, 20, 20), False),
    ((10, 30, 20), False),
    ((40, 10, 20, 30), True),
    ((10, 20, 30, 15), False),
], ids=["three-rising", "only-two", "equal", "decrease", "last-three-rise", "old-rise-ended"])
def test_facts_baseline_trend_requires_three_strictly_rising_successes(baseline_facts, durations, rising):
    case = baseline_facts
    for index, duration in enumerate(durations):
        case.seed(index * 3 + 1, duration)
        case.seed(index * 3 + 2, 9999, timed_out=True)
        case.seed(index * 3 + 3, None)  # Older producer: duration is unavailable.
    limit = case.capture_limit()
    assert durations[-1] / limit < 0.75, "Keep this fixture below the ratio warning so it isolates the growth warning"

    trend = case.read()["baseline_trend"]
    assert trend["latest_duration_seconds"] == durations[-1]
    assert trend["capture_limit_seconds"] == limit
    assert trend["ratio"] == pytest.approx(durations[-1] / limit)
    assert trend["near_limit"] is rising, (
        "Use the last three measured successes: skip timeouts/unknown durations; equal values and old rising streaks do not qualify")


def test_facts_baseline_trend_lists_legacy_duration_as_unknown_beside_measured_builds(baseline_facts):
    case = baseline_facts
    legacy = case.seed(1, None)
    measured = case.seed(2, 195)

    result = case.read()
    rows = result["baseline_builds"]
    assert [{field: row[field] for field in legacy} for row in rows] == [measured, legacy]
    assert result["baseline_trend"]["latest_duration_seconds"] == 195


@pytest.mark.parametrize("history", ["empty", "legacy-only", "timeout-only"])
def test_facts_baseline_trend_without_measured_success_has_no_ratio(baseline_facts, history):
    case = baseline_facts
    expected = []
    if history == "legacy-only":
        expected.append(case.seed(1, None))
    elif history == "timeout-only":
        expected.append(case.seed(1, 900, timed_out=True))
    limit = case.capture_limit()

    result = case.read()
    assert len(result["baseline_builds"]) == len(expected)
    for row, wanted in zip(result["baseline_builds"], expected, strict=True):
        assert {field: row[field] for field in wanted} == wanted
    assert result["baseline_trend"] == {
        "latest_duration_seconds": None, "capture_limit_seconds": limit,
        "ratio": None, "near_limit": False,
    }, "An unknown duration is not a zero-second capture or a measured trend"
    if history == "empty":
        assert not case.cache.exists(), "Reading an absent cache must not create it"


@pytest.mark.parametrize("durations", [(620, 610, 600), (695, 685, 675), (10, 20, 30)])
def test_facts_baseline_trend_human_line_names_latest_duration_share_and_warning(baseline_facts, durations):
    case = baseline_facts
    for index, duration in enumerate(durations, 1):
        case.seed(index, duration)
    case.seed(4, 9999, timed_out=True)
    limit = case.capture_limit()
    latest = durations[-1]
    near = latest / limit >= 0.75 or durations[0] < durations[1] < latest

    output = case.read(human=True)
    lines = [line for line in output.splitlines()
             if re.search(r"baseline", line, re.I)
             and re.search(rf"\b{latest}(?:\.0+)?\s*(?:s\b|seconds?\b)", line)]
    assert len(lines) == 1, f"Name the latest successful capture duration and its limit share in one line: {output}"
    percentages = re.findall(r"(\d+(?:\.\d+)?)\s*%", lines[0])
    assert len(percentages) == 1, f"Show the capture's share of the current limit as a percentage: {lines[0]}"
    assert float(percentages[0]) == pytest.approx(100 * latest / limit, abs=0.5)
    assert bool(re.search(r"\bnear(?:[ _-]+the)?[ _-]+limit\b", lines[0], re.I)) is near


def test_facts_baseline_trend_repeated_json_and_human_observation_is_read_only(baseline_facts):
    case = baseline_facts
    case.seed(1, None)
    case.seed(2, 485)
    case.seed(3, 736)

    first = case.read()
    assert first["baseline_builds"][0]["duration_seconds"] == 736
    assert case.read() == first
    human = case.read(human=True)
    assert case.read(human=True) == human
    assert case.read() == first, "Human rendering must not warm, rebuild, annotate or rewrite the cache"
