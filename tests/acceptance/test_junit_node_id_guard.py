"""Regression guard for junit-node-id (not in Done when): ordinary ids stay exactly as today."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from foragent import runner
from junit_reimport_world import DEFINING, DEFINING_ID, cases, junit_report, make_project, rerun


@pytest.mark.parametrize(("attributes", "expected"), [
    ({"file": "tests/acceptance/test_a.py", "classname": "tests.acceptance.test_a", "name": "test_x"},
     "tests/acceptance/test_a.py::test_x"),
    ({"file": "tests/acceptance/test_a.py", "classname": "tests.acceptance.test_a.TestK", "name": "test_y[p-1]"},
     "tests/acceptance/test_a.py::TestK::test_y[p-1]"),
    ({"file": "tests/acceptance/test_a.py", "classname": "tests.acceptance.test_a.TestK.TestInner", "name": "test_z"},
     "tests/acceptance/test_a.py::TestK::TestInner::test_z"),
    ({"file": "test_top.py", "classname": "test_top", "name": "test_q"}, "test_top.py::test_q"),
])
def test_ordinary_ids_unchanged(attributes, expected):
    assert runner.junit_node_id(ET.Element("testcase", attributes)) == expected


@pytest.mark.parametrize("attributes", [
    {"classname": "tests.acceptance.test_a", "name": "test_x"},
    {"file": "tests/acceptance/test_a.py", "classname": "tests.acceptance.test_a"},
])
def test_incomplete_cases_have_no_id(attributes):
    assert runner.junit_node_id(ET.Element("testcase", attributes)) is None


def test_defining_copy_keeps_its_ordinary_selectable_id(tmp_path):
    root = make_project(tmp_path)
    report = junit_report(root)
    module = DEFINING.removesuffix(".py").replace("/", ".")
    own = [c for c in cases(report) if c.get("classname") == module and c.get("name") == "test_shared_budget[parallel]"]
    assert len(own) == 1
    assert runner.junit_node_id(own[0]) == DEFINING_ID
    result, ran = rerun(root, DEFINING_ID, "own.xml")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [(c.get("classname"), c.get("name")) for c in ran] == [(module, "test_shared_budget[parallel]")]
