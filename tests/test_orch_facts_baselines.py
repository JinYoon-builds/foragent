"""`fa orch facts` baseline rows: zero-test captures, legacy estimates and the facts window."""

from datetime import timedelta
import json
import xml.etree.ElementTree as ET

from conftest import BASE_TIME
from foragent import orch_facts_baselines

WINDOW = timedelta(days=7)


def seed(cache, name, *, age, duration=None, count=1, repository="/repo", **extra):
    cache.mkdir(exist_ok=True)
    saved = {"node_ids": ["t::a"] * count, "created_at": (BASE_TIME - age).isoformat(), **extra}
    if repository is not None:
        saved["repository"] = repository
    if duration is not None:
        saved["duration_seconds"] = duration
    (cache / f"{name}.json").write_text(json.dumps(saved))
    return cache / f"{name}.json"


def collect(cache, repo=None):
    return orch_facts_baselines.collect(cache, "/repo", repo, BASE_TIME, WINDOW)


def test_zero_test_success_is_listed_with_unknown_duration_and_not_measured(tmp_path):
    seed(tmp_path / "b", "zero", age=timedelta(minutes=1), duration=50, count=0)
    seed(tmp_path / "b", "real", age=timedelta(minutes=2), duration=100)
    facts = collect(tmp_path / "b")
    assert [(row["key"], row["test_count"], row["duration_seconds"]) for row in facts["baseline_builds"]] == [
        ("zero", 0, None), ("real", 1, 100)]
    assert facts["baseline_trend"]["latest_duration_seconds"] == 100


def test_legacy_entry_shows_estimated_junit_time_and_counts_as_measured(tmp_path):
    path = seed(tmp_path / "b", "legacy", age=timedelta(minutes=1), repository=None)
    suite = ET.Element("testsuite", time="321.5")
    ET.ElementTree(suite).write(path.with_suffix(".xml"), encoding="unicode")
    facts = collect(tmp_path / "b")
    row = facts["baseline_builds"][0]
    assert row["duration_seconds"] == 321.5 and row["duration_estimated"] is True
    assert facts["baseline_trend"]["latest_duration_seconds"] == 321.5


def test_rows_and_trend_stay_inside_the_window_but_the_limit_uses_all_history(tmp_path):
    seed(tmp_path / "b", "old", age=timedelta(days=8), duration=1000)
    facts = collect(tmp_path / "b")
    assert facts["baseline_builds"] == []
    assert facts["baseline_trend"] == {"latest_duration_seconds": None, "capture_limit_seconds": 2000,
                                       "ratio": None, "near_limit": False}


def test_slug_counts_only_without_a_repository_path(tmp_path):
    seed(tmp_path / "b", "slug", age=timedelta(minutes=1), duration=10, repository=None, repo="o/r")
    seed(tmp_path / "b", "elsewhere", age=timedelta(minutes=2), duration=20, repository="/other", repo="o/r")
    assert [row["key"] for row in collect(tmp_path / "b", "o/r")["baseline_builds"]] == ["slug"]
    assert collect(tmp_path / "b")["baseline_builds"] == []


def test_human_line_in_korean_names_duration_share_and_warning(tmp_path):
    seed(tmp_path / "b", "near", age=timedelta(minutes=1), duration=700)
    line = orch_facts_baselines.human_line(collect(tmp_path / "b"), "ko")
    assert line == "기준선 캡처: 최근 700 s, 한도 1400 s의 50%"
    seed(tmp_path / "c", "big", age=timedelta(minutes=1), duration=3000)
    assert orch_facts_baselines.human_line(collect(tmp_path / "c"), "en") == (
        "Baseline capture: latest 3000 s, 83% of the 3600 s limit; near the limit")
    assert "no measured capture yet" in orch_facts_baselines.human_line(collect(tmp_path / "d"), "en")


import pytest


@pytest.mark.parametrize("report", [
    '<testsuite name="no-time"/>', '<testsuite time="soon"/>', "<testsuite", "<other/>",
], ids=["missing-time", "non-numeric-time", "malformed", "wrong-root"])
def test_legacy_entry_with_unusable_xml_stays_listed_with_unknown_duration(tmp_path, report):
    path = seed(tmp_path / "b", "legacy", age=timedelta(minutes=1), repository=None)
    path.with_suffix(".xml").write_text(report)
    measured = seed(tmp_path / "b", "measured", age=timedelta(minutes=2), duration=50)
    facts = collect(tmp_path / "b")
    assert [(row["key"], row["duration_seconds"], row["duration_estimated"]) for row in facts["baseline_builds"]] == [
        ("legacy", None, False), ("measured", 50, False)]
    assert facts["baseline_trend"]["latest_duration_seconds"] == 50


def test_entry_with_malformed_node_ids_is_listed_with_zero_tests(tmp_path):
    seed(tmp_path / "b", "odd", age=timedelta(minutes=1), duration=40, node_ids=7)
    assert [(row["key"], row["test_count"]) for row in collect(tmp_path / "b")["baseline_builds"]] == [("odd", 0)]
