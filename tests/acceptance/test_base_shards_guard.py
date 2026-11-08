"""Regression controls for the sharded baseline (outside Done when; pass on main and after).

They prove the offline push simulator reproduces today's published base
cache, so a red result in test_base_shards.py is about the workflow, not the
simulator.
"""

import pytest

from base_shards_case import ALL_NODES, EXPECTED_RESULTS, Project, run_log


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    return Project(tmp_path_factory.mktemp("base-shards-guard"))


def test_simulated_main_push_publishes_every_collected_node_outcome(project):
    document = project.generated_workflow()
    keys = project.pr_restore_keys(document)
    run = project.push(document)
    executed = [node for nodes in run.executed.values() for node in nodes]
    assert sorted(executed) == sorted(ALL_NODES), run_log(run)
    published = [key for key in run.caches if key in keys]
    assert len(published) == 1, run_log(run)
    assert project.published_results(run, published[0]) == EXPECTED_RESULTS


def test_simulated_main_push_without_any_measurement_publishes_nothing(project):
    document = project.generated_workflow()
    keys = project.pr_restore_keys(document)
    clean = project.push(document)
    crashed = {index: "crash" for index, record in enumerate(clean.instances)
               if record in clean.measuring}
    assert crashed, run_log(clean)
    run = project.push(document, fault=crashed)
    assert not [key for key in run.caches if key in keys], run_log(run)
