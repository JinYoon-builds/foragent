"""Acceptance test for junit-node-id: a test collected by another module than its file
yields a node id that real pytest can select."""

from __future__ import annotations

from foragent import runner
from junit_reimport_world import (
    COLLECTING, COLLECTING_ID, DEFINING, DEFINING_ID, cases, junit_report, make_project, rerun,
)


def _copy(report, module_file):
    module = module_file.removesuffix(".py").replace("/", ".")
    found = [case for case in cases(report)
             if case.get("name") == "test_shared_budget[parallel]" and case.get("classname") == module]
    assert len(found) == 1, [(c.get("classname"), c.get("name")) for c in cases(report)]
    return found[0]


def test_reimported_copy_gets_an_id_that_selects_and_runs_it(tmp_path):
    root = make_project(tmp_path)
    report = junit_report(root)
    copy = _copy(report, COLLECTING)
    # The scenario of #2768: JUnit names the defining file for the collecting module's copy.
    assert copy.get("file") == DEFINING

    node_id = runner.junit_node_id(copy)
    assert node_id == COLLECTING_ID

    result, ran = rerun(root, node_id, "rerun.xml")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(ran) == 1, [(c.get("classname"), c.get("name")) for c in ran]
    assert ran[0].get("classname") == COLLECTING.removesuffix(".py").replace("/", ".")
    assert ran[0].get("name") == "test_shared_budget[parallel]"
    assert all(ran[0].find(tag) is None for tag in ("failure", "error", "skipped"))


def test_every_passing_id_from_the_report_is_selectable_and_both_copies_are_kept(tmp_path):
    root = make_project(tmp_path)
    report = junit_report(root)
    ids = runner._passing_acceptance_ids(report)

    assert len(ids) == len(set(ids))
    assert set(ids) == {
        DEFINING_ID,
        COLLECTING_ID,
        f"{DEFINING}::test_plain",
        f"{DEFINING}::TestGroup::test_inside",
        f"{COLLECTING}::test_guard_own",
    }
    # A's own copy keeps its ordinary id beside B's copy.
    assert runner.junit_node_id(_copy(report, DEFINING)) == DEFINING_ID

    for index, node_id in enumerate(sorted(ids)):
        result, ran = rerun(root, node_id, f"each-{index}.xml")
        assert result.returncode == 0, (node_id, result.stdout + result.stderr)
        assert len(ran) == 1, (node_id, [(c.get("classname"), c.get("name")) for c in ran])
