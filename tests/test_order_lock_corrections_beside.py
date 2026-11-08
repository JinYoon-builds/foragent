"""Main-only preflight must not judge corrections that land beside an issue (r1210, #2841).

The collection and importer check (``order_collect.check``) runs a changed file's importers on main
and refuses when one turns red. A corrected helper that turns a main-green test red is exactly the
correction that belongs in the issue's PR, so every caller leaves such files out of that check; the
publication check proves them on the branch and on main instead. The conftest switches the real
check off here, so ``_importer_check`` stands in with the same refusal for the corrected helper.
"""

from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from lock_corrections_beside_case import (BRANCH, CLOSED, HELPER, NEW_HELPER, NUMBER, OLD_HELPER,  # noqa: E402,F401
                                          apply, case, commits_touching, correction, describe,
                                          offline_publication, on_origin, publish_and_check, world)
from foragent import order, order_collect  # noqa: E402
from foragent.order import PlanError  # noqa: E402


def _importer_check(monkeypatch):
    """Refuse like the importer check does when the corrected helper breaks a main-green importer."""
    seen = []

    def check(plan, files, root, config, git=None, *, base=None, earlier=None):
        seen.append(sorted(files))
        if files.get(HELPER) == NEW_HELPER:
            raise PlanError(f"importers refused: {HELPER} breaks {CLOSED}")
        return None

    monkeypatch.setattr(order_collect, "check", check)
    return seen


def test_a_main_red_helper_correction_reaches_the_issue_branch(world, monkeypatch):
    world.outcomes[("main", CLOSED, "old", "new")] = False  # The corrected helper breaks closed #5 on main.
    seen = _importer_check(monkeypatch)
    plan = correction({HELPER: NEW_HELPER})
    row = publish_and_check(world, plan)
    assert row is not None and row["status"] == "pending" and row.get("red_check_commit"), describe(world, row)
    assert seen and all(HELPER not in names for names in seen), seen
    assert row["red_checks"][f"#{NUMBER}"]["lands_on"] == "branch"
    apply(world, plan)
    assert on_origin(world, "main", HELPER) == OLD_HELPER, "Main stays green"
    assert on_origin(world, BRANCH, HELPER) == NEW_HELPER, "The correction lands in the issue's PR"
    assert commits_touching(world, BRANCH, HELPER, since=world.branch_head) == 1


def test_a_dry_run_previews_a_main_red_helper_correction_without_refusing_it(world, monkeypatch):
    world.outcomes[("main", CLOSED, "old", "new")] = False
    seen = _importer_check(monkeypatch)
    case = world.case
    order.publish(correction({HELPER: NEW_HELPER}), world.root, case.config, case.tracker, repo=case.config.repo,
                  git=world.git, approve=True, approve_numbers=[NUMBER], dry_run=True)
    assert seen and all(HELPER not in names for names in seen), seen


def test_a_direct_publication_refuses_a_beside_correction(world):
    case = world.case
    with pytest.raises(PlanError, match="merge_owner"):
        order.publish(correction({HELPER: NEW_HELPER}), world.root, replace(case.config, merge_owner=False),
                      case.tracker, repo=case.config.repo, git=world.git, approve=True, approve_numbers=[NUMBER])
    assert on_origin(world, "main", HELPER) == OLD_HELPER
