"""Acceptance test for ci-exclusions-drift. Locked by Foragent; do not edit."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from foragent.ci_mode import UnusableComparison, comparison
from foragent.ci_workflow import measurement_scope
from test_ci_mode import (
    EXCLUDED,
    FUTURE,
    KEEP,
    OTHER,
    REPO,
    github_fixture,
    isolated_home,
    no_external_execution,
    offline_acceptance,
)


NEW_LOCKED = FUTURE.split("::", 1)[0]
REQUIRED = KEEP.split("::", 1)[0]
NOW_EXCLUDED = sorted([*EXCLUDED, NEW_LOCKED])
DURATION = 13.5


@pytest.fixture
def recorded_comparison(tmp_path):
    """Two complete shards retain the exclusions recorded when CI started.

    EXCLUDED represents the pushed Foragent-Acceptance-Excluded trailer. A
    later order changes the gate's selection without changing either SHA.
    Only captured GitHub records and the existing offline helpers are used.
    """
    config = SimpleNamespace(
        acceptance_dir="tests/acceptance/", test_command="python -m pytest", ci_shards=2,
    )
    run = github_fixture("measurement_run.json")
    template = github_fixture("shard-0.json")
    artifacts = {item["name"]: item for item in github_fixture("artifacts.json")["artifacts"]}
    scope = measurement_scope(config, tmp_path, EXCLUDED)
    reports = []
    for index, nodes in enumerate(([KEEP, FUTURE], [OTHER])):
        payload = deepcopy(template)
        payload.update(
            repository=REPO, head_sha=run["head_sha"], shard_index=index, shard_count=2,
            excluded=list(EXCLUDED), scope=deepcopy(scope), duration_seconds=12.5 + index,
        )
        for side in ("base", "head"):
            payload[side].update(complete=True, collected_ids=list(nodes), passing_ids=list(nodes))
        artifact = deepcopy(artifacts[f"acceptance-{index}"])
        artifact["workflow_run"].update(id=run["id"], head_sha=run["head_sha"])
        reports.append({"run": deepcopy(run), "artifact": artifact, "report": payload})
    return SimpleNamespace(
        reports=reports, base_sha=template["base_sha"], head_sha=run["head_sha"],
        config=config, root=tmp_path,
    )


def compare(recorded, excluded, reports=None):
    return comparison(
        recorded.reports if reports is None else reports,
        REPO, recorded.base_sha, recorded.head_sha, excluded,
        expected_scope=measurement_scope(recorded.config, recorded.root, excluded),
        expected_shards=range(recorded.config.ci_shards),
    )


def test_ci_exclusions_drift_new_order_ignores_its_newly_locked_failure(recorded_comparison):
    recorded = recorded_comparison
    recorded.reports[0]["report"]["head"]["passing_ids"].remove(FUTURE)

    # #1240 (2026-10-31, test owner): CI measured this test (it was not excluded
    # when CI ran), so its regression still blocks after a later order locks the
    # file. The evidence stays usable.
    assert compare(recorded, EXCLUDED) == ([FUTURE], DURATION)
    assert compare(recorded, NOW_EXCLUDED) == ([FUTURE], DURATION)


def test_ci_exclusions_drift_cannot_omit_a_currently_required_file(recorded_comparison):
    recorded = recorded_comparison
    uncovered = deepcopy(recorded.reports)
    for evidence in uncovered:
        payload = evidence["report"]
        payload["excluded"] = sorted([*EXCLUDED, REQUIRED])
        payload["scope"]["excluded"] = list(payload["excluded"])
        # Faithfully represent CI not collecting the file it excluded. Reject
        # insufficient coverage even if every test CI did collect passed.
        for side in ("base", "head"):
            for key in ("collected_ids", "passing_ids"):
                payload[side][key] = [node for node in payload[side][key] if node != KEEP]

    # #1240 (2026-10-31, test owner): the exclusions CI recorded are the
    # reference. A file CI excluded is not protected by this comparison even if
    # today's list no longer excludes it; the evidence stays usable.
    for excluded in (EXCLUDED, NOW_EXCLUDED):
        assert compare(recorded, excluded, uncovered) == ([], DURATION)

    # Pair rejection guards with valid drift evidence so rejecting all changed
    # exclusion lists cannot satisfy this test.
    assert compare(recorded, NOW_EXCLUDED) == ([], DURATION)


def test_ci_exclusions_drift_equal_sets_keep_protected_regressions(recorded_comparison):
    recorded = recorded_comparison
    assert compare(recorded, EXCLUDED) == ([], DURATION)

    recorded.reports[0]["report"]["head"]["passing_ids"].remove(KEEP)
    assert compare(recorded, EXCLUDED) == ([KEEP], DURATION)
    # Growing exclusions only waives tests in the newly locked file; a passing
    # base test in another file must still be reported as a regression.
    assert compare(recorded, NOW_EXCLUDED) == ([KEEP], DURATION)


def test_ci_exclusions_drift_stale_base_and_other_identity_changes_stay_rejected(recorded_comparison):
    recorded = recorded_comparison
    run_id = recorded.reports[1]["run"]["id"]
    changes = (
        (("report", "base_sha"), "0" * 40),
        (("report", "head_sha"), "1" * 40),
        (("report", "repository"), "another/repository"),
        (("report", "scope", "acceptance_dir"), "tests/other/"),
        (("report", "scope", "command"), "python -m pytest tests/other"),
        (("report", "scope", "marker"), "local_only"),
        (("report", "scope", "shard_count"), 3),
        (("report", "scope", "excluded"), sorted([*EXCLUDED, REQUIRED])),
        (("report", "shard_count"), 3),
        (("report", "shard_index"), 0),
        (("run", "head_sha"), "1" * 40),
        (("run", "repository", "full_name"), "another/repository"),
        (("run", "head_repository", "full_name"), "another/repository"),
        (("artifact", "workflow_run", "id"), run_id + 1),
    )
    for path, value in changes:
        damaged = deepcopy(recorded.reports)
        target = damaged[1]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        # Equal-list controls prove the changed field itself is invalid. The
        # same mismatch, including a stale base on just one shard, stays fatal
        # when a newly published order expands the current exclusions.
        for excluded in (EXCLUDED, NOW_EXCLUDED):
            with pytest.raises(UnusableComparison):
                compare(recorded, excluded, damaged)

    assert compare(recorded, NOW_EXCLUDED) == ([], DURATION)
