"""Acceptance test for night-ci-evidence. Locked by Foragent; do not edit."""

from copy import deepcopy
from pathlib import Path

import pytest

from foragent.ci_mode import UnusableComparison, comparison
from foragent.ci_workflow import WORKFLOW, measurement_scope
from foragent.config import Config
from test_ci_mode import (
    assert_infrastructure_hold, assert_remote_only, mode_case, no_external_execution, run,
)
from test_ci_mode_unit import report
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


EXPECTED_BASE = "required-base-3f8b"
EXPECTED_HEAD = "required-head-7e2c"


def good_report(index=0, count=1):
    value = report(index=index, count=count)
    value["run"]["head_sha"] = value["artifact"]["workflow_run"]["head_sha"] = EXPECTED_HEAD
    value["report"].update(base_sha=EXPECTED_BASE, head_sha=EXPECTED_HEAD)
    return value


def compare(values, count=1):
    return comparison(values, "owner/repo", EXPECTED_BASE, EXPECTED_HEAD, [],
                      expected_scope=measurement_scope(Config(ci_shards=count), Path("."), []),
                      expected_shards=range(count))


@pytest.mark.parametrize("path,actual,expected", [
    (("run", "id"), 0, "positive"),
    (("run", "head_sha"), "stale-run-head", EXPECTED_HEAD),
    (("run", "status"), "queued", "completed"),
    (("run", "conclusion"), "failure", "success"),
    (("run", "event"), "push", "pull_request"),
    (("run", "path"), ".github/workflows/foreign.yml", WORKFLOW),
    (("run", "repository", "full_name"), "foreign/source", "owner/repo"),
    (("run", "head_repository", "full_name"), "foreign/head", "owner/repo"),
    (("artifact", "id"), 0, "positive"),
    (("artifact", "expired"), True, "False"),
    (("artifact", "workflow_run", "id"), 43, "42"),
    (("artifact", "workflow_run", "head_sha"), "foreign-artifact-head", EXPECTED_HEAD),
    (("report", "schema_version"), 99, "1"),
    (("report", "repository"), "foreign/report", "owner/repo"),
    (("report", "base_sha"), "previous-base", EXPECTED_BASE),
    (("report", "head_sha"), "previous-head", EXPECTED_HEAD),
    (("report", "excluded"), ["extra-lock.py"], "[]"),
    (("report", "scope", "marker"), "local_only", "not local_only"),
    (("report", "shard_index"), 3, "0"),
    (("report", "shard_count"), 2, "1"),
    (("artifact", "name"), "acceptance-99", "acceptance-0"),
])
def test_shard_rejection_names_the_field_and_expected_and_observed_values(path, actual, expected):
    value = good_report()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = actual
    with pytest.raises(UnusableComparison) as caught:
        compare([value])
    message = str(caught.value)
    assert ".".join(path) in message, message
    assert expected.casefold() in message.casefold(), message
    observed = actual[0] if isinstance(actual, list) else str(actual)
    assert observed.casefold() in message.casefold(), message


def test_multiple_invalid_fields_report_the_first_encountered_check():
    value = good_report()
    value["run"]["head_sha"] = "stale-run-head"
    value["report"]["base_sha"] = "previous-base"
    with pytest.raises(UnusableComparison) as caught:
        compare([value])
    message = str(caught.value)
    assert "run.head_sha" in message and "stale-run-head" in message and EXPECTED_HEAD in message, message


@pytest.mark.parametrize("duplicate", ["artifact", "shard", "producer"])
def test_cross_shard_identity_failures_also_show_field_and_both_values(duplicate):
    values = [good_report(0, count=2), good_report(1, count=2)]
    if duplicate == "artifact":
        values[1]["artifact"]["id"] = values[0]["artifact"]["id"]
        field, actual, expected = "artifact.id", "100", "unique"
    elif duplicate == "shard":
        values[1]["report"]["shard_index"] = 0
        values[1]["artifact"]["name"] = "acceptance-0"
        field, actual, expected = "report.shard_index", "0", "unique"
    else:
        values[1]["run"]["id"] = values[1]["artifact"]["workflow_run"]["id"] = 43
        field, actual, expected = "run.id", "43", "42"
    with pytest.raises(UnusableComparison) as caught:
        compare(values, count=2)
    message = str(caught.value)
    assert field in message and actual in message and expected in message.casefold(), message


@pytest.mark.parametrize("stale", ["moved-base", "superseded-producer"])
def test_only_stale_identity_waits_for_fresh_ci_then_merges_without_worker_retry(
    git_history, tmp_path, monkeypatch, stale,
):
    case = mode_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.tracker.pr_acceptance_reports
    reads = []

    def reports(head):
        values = [deepcopy(value) for value in original(head)]
        reads.append(values)
        if len(reads) == 1:
            if stale == "moved-base":
                for value in values:
                    value["report"]["base_sha"] = "previous-base"
            else:
                # Two complete trusted reports straddle a measurement rerun.
                # The retry supplies a complete set from one current producer.
                values[0]["run"]["id"] -= 1
                values[0]["artifact"]["workflow_run"]["id"] -= 1
        return values

    monkeypatch.setattr(case.ctx.tracker, "pr_acceptance_reports", reports)
    outcome = run(case)
    assert outcome == "merged", outcome
    assert len(reads) >= 2, "Stale evidence must be replaced before the merge"
    assert case.tracker.merged_prs == [2]
    assert len(case.implementer.calls) == 1
    assert case.store.load_flag(1) is None
    assert_remote_only(case)


def test_stale_base_does_not_make_an_unrelated_invalid_field_retryable(git_history, tmp_path, monkeypatch):
    case = mode_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.tracker.pr_acceptance_reports
    reads = []

    def reports(head):
        values = deepcopy(original(head))
        reads.append(values)
        values[0]["report"]["base_sha"] = "previous-base"
        values[1]["report"]["repository"] = "foreign/repository"
        return values

    monkeypatch.setattr(case.ctx.tracker, "pr_acceptance_reports", reports)
    assert_infrastructure_hold(case, run(case))
    assert len(reads) == 1, "Only freshness mismatches can use the freshness retry"
    assert_remote_only(case)


def test_persistent_stale_base_uses_existing_ci_wait_budget_then_holds(git_history, tmp_path, monkeypatch):
    case = mode_case(git_history, tmp_path, monkeypatch)
    original = case.ctx.tracker.pr_acceptance_reports
    reads = []
    sleeps = []
    sleep = case.ctx.sleep

    def reports(head):
        values = deepcopy(original(head))
        reads.append(values)
        for value in values:
            value["report"]["base_sha"] = "previous-base"
        return values

    def wait(seconds):
        sleeps.append(seconds)
        sleep(seconds)

    monkeypatch.setattr(case.ctx.tracker, "pr_acceptance_reports", reports)
    monkeypatch.setattr(case.ctx, "sleep", wait)
    assert_infrastructure_hold(case, run(case))
    assert len(reads) > 1 and sleeps, "A moved base must receive a chance to refresh CI"
    assert sum(sleeps) <= case.config.max_runtime_minutes * 60, "Freshness recovery remains bounded"
    assert_remote_only(case)
