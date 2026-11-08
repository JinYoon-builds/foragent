"""Acceptance for r1222: the main-push baseline runs as shards and merges one base cache.

Each test runs one offline main push of the workflow that `fa ci workflow`
generates (and the committed workflow file), through base_shards_sim: every
job instance has its own checkout and runner.temp, so shard reports reach the
merge step only through the workflow's own artifact or cache steps. The
published base cache is read with the same reader the PR comparison uses.
"""

import pytest

from base_shards_case import (ALL_NODES, EXPECTED_RESULTS, SHARDS, Project, expected_partition, run_log)
from base_shards_sim import transitive_needs


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    return Project(tmp_path_factory.mktemp("base-shards"))


@pytest.fixture(scope="module")
def generated(project):
    document = project.generated_workflow()
    return document, project.push(document)


def base_cache_entries(project, document, run):
    keys = project.pr_restore_keys(document)
    assert keys, "PR acceptance shards must still restore a base cache key"
    return [key for key in run.caches if key in keys]


def assert_sharded(project, document, run):
    """Shared shape: more than one baseline shard job, PR shard assignment, one merged cache."""
    measuring = run.measuring
    assert len(measuring) > 1, (
        f"The main-push baseline must run as more than one shard job; measuring jobs: "
        f"{[record['label'] for record in measuring]}\n{run_log(run)}")
    assert len(measuring) == SHARDS, "Baseline shards must use the PR acceptance shard layout (ci_shards)"
    executed = [node for record in measuring for node in run.executed[record["label"]]]
    assert sorted(executed) == sorted(ALL_NODES), "Every collected node runs exactly once across the shards"
    partition = sorted(sorted(nodes) for nodes in expected_partition().values())
    assert sorted(sorted(run.executed[record["label"]]) for record in measuring) == partition, (
        "Each baseline shard must measure exactly one PR acceptance shard's files")
    entries = base_cache_entries(project, document, run)
    assert len(entries) == 1, f"One base cache must be published, got {entries}\n{run_log(run)}"
    owner = run.caches[entries[0]]["owner"]
    labels = {record["label"] for record in measuring}
    assert owner not in labels, "A shard job must not publish its partial result as the base cache"
    saving_job = next(record["job"] for record in run.instances if record["label"] == owner)
    assert {record["job"] for record in measuring} <= transitive_needs(document["jobs"], saving_job), (
        "The merge step must wait for every baseline shard job")
    attempts = [save for save in run.cache_saves if save["key"] == entries[0]]
    assert len(attempts) == 1, f"Exactly one step publishes the base cache, got {attempts}"
    return entries[0]


@pytest.mark.parametrize("source", ["generated", "committed"])
def test_main_push_runs_shard_jobs_and_merges_one_full_base_cache(project, generated, source):
    """Reject a single baseline job, a shard-published partial cache, a different shard
    assignment, a merge that drops, flips or doubles a shard's nodes, and a stale committed file."""
    if source == "generated":
        document, run = generated
    else:
        document = project.committed_workflow()
        run = project.push(document)
    key = assert_sharded(project, document, run)
    assert project.published_results(run, key) == EXPECTED_RESULTS, (
        "The merged base cache must hold every collected node's outcome, as one unsharded run")


@pytest.mark.parametrize("fault", ["crash", "lost-report", "other-shard-report"])
def test_a_shard_without_its_report_publishes_no_base_cache(project, generated, fault):
    """Reject a merge that publishes a partial base cache when one baseline shard has no report:
    the shard job crashed, it finished but its report never reached the merge step, or the
    merge step received a second copy of another shard's report in its place."""
    document, clean = generated
    assert_sharded(project, document, clean)
    missing = clean.measuring[1]
    index = clean.instances.index(missing)
    injected = {"crash": "crash", "lost-report": "lose-publications",
                "other-shard-report": ("replace-reports-with", clean.measuring[0]["label"])}[fault]
    run = project.push(document, fault={index: injected})
    assert run.instances[index]["label"] == missing["label"]
    assert not base_cache_entries(project, document, run), (
        f"No base cache may be published while shard {missing['label']} has no report\n{run_log(run)}")
