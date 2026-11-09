"""Returned red facts retain review only for an edit's declared command removals."""

from copy import deepcopy
from dataclasses import replace

import pytest

from foragent import order, order_prepare, order_prepare_cycle, order_prepare_setup, order_publications, order_reviewed
from test_order_publications import COMMAND, NAME, case, submit  # noqa: F401

OLD = "uv run pytest tests/test_existing.py"


@pytest.fixture
def returned_edit(case):
    number = case.tracker.create_issue("Existing work", f"## Done when\n```sh\n{OLD}\n```\n", ["lane:main"])
    source = case.plan["issues"].pop()
    edit = {name: deepcopy(source[name]) for name in ("goal", "out_of_scope", "statements", "acceptance_files")}
    edit.update(number=number, done_when=[OLD, COMMAND])
    case.plan["edits"] = [edit]
    files = order.validate_plan(case.plan, case.root, case.config)
    view = order.edit_view(edit, case.tracker.get_issue(number), case.config)
    entry = {"key": view["key"], "status": "reviewed", "written": deepcopy(edit["acceptance_files"]),
             "done_when": list(edit["done_when"]), "base_revision": "main-1", "others": {},
             "cycle": order_prepare_cycle.identity(case.plan, view, case.config, case.root, files),
             "published": order_prepare_setup.published_digest(case.plan, case.root, case.config, files)}
    path = order_prepare._record_path(case.store, case.plan)
    order_prepare._write(path, {"issues": {view["key"]: entry}})
    row = {"id": "returned", "plan": deepcopy(case.plan), "prepared": {view["key"]: deepcopy(entry)},
           "red_checks": {view["key"]: {**deepcopy(entry), "status": "hold", "reason": "Regression is green"}}}
    order_publications._return_to_preparation(case.store, row)
    case.reviewed = entry
    case.edit, case.key = edit, view["key"]
    edit["done_when"] = [COMMAND]
    edit["done_when_removals"] = [{"line": OLD, "reason": "Covered by regression CI"}]
    return case


def test_declared_removal_reuses_review_without_mutating_the_preparation_record(returned_edit):
    case = returned_edit
    path = order_prepare._record_path(case.store, case.plan)
    before = path.read_bytes()
    submit(case)
    queued, = order_publications.pending(case.store)
    assert queued["prepared"][case.key]["done_when"] == [COMMAND]
    assert queued["prepared"][case.key]["written"] == case.reviewed["written"]
    assert path.read_bytes() == before
    assert not case.checks  # The off-owner publication pass still has to measure red facts.


@pytest.mark.parametrize("change", ["tests", "commands", "undeclared", "goal", "spec", "setup", "peers"])
def test_a_return_does_not_admit_other_unreviewed_changes(returned_edit, change):
    case = returned_edit
    if change == "tests":
        case.edit["acceptance_files"][NAME] += "# different bytes\n"
    elif change == "commands":
        case.edit["done_when"].append("uv run pytest tests/test_unreviewed.py")
    elif change == "undeclared":
        case.edit.pop("done_when_removals")
    elif change == "goal":
        case.edit["goal"] += " and change another behaviour"
    elif change == "spec":
        case.plan["spec_markdown"] += "\nChanged requirements"
    elif change == "setup":
        case.config = replace(case.config, language="ko")
    elif change == "peers":
        path = order_prepare._record_path(case.store, case.plan)
        record = order_prepare._read(path, case.plan)
        record["issues"][case.key]["returned_reviewed"]["others"] = {"tests/acceptance/peer.py": "old"}
        order_prepare._write(path, record)
    with pytest.raises(order.PlanError):
        submit(case)
    assert not order_publications.requests(case.store)


def test_a_return_does_not_reuse_review_for_a_new_issue(case):
    item = case.plan["issues"][0]
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    reviewed = record["issues"]["new"]
    record["issues"]["new"] = {"status": "hold", "returned_reviewed": reviewed}
    item["done_when"] = []
    item["done_when_removals"] = [{"line": COMMAND, "reason": "Removed"}]
    with pytest.raises(order.PlanError, match="have no reviewed tests"):
        order_reviewed.require_reviewed_tests(case.plan, case.root, case.config, case.config.repo, [item],
                                              record=record["issues"])
