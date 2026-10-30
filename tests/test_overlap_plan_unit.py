"""Per-file overlap limits in the plan (fa orch plan --overlap): storage, admission, late files."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from conftest import BASE_TIME
from foragent import orch_plan
from foragent.overlap_files import can_start, file_limits, limit_for
from foragent.run_overlap import wait_for_overlap
from test_overlap_files import context, edit, reserve  # noqa: F401
from test_run_overlap import attempt, clock

WHY = "12 overlaps, 0 conflicts, 0 redo"


def plan_file(context, path, limit, *, by="orchestrator", expires=None, case_sensitive=True):
    return orch_plan.set_files(context.store, {path: limit}, WHY, expires, by, case_sensitive=case_sensitive)


def test_file_limit_replaces_the_fallback_only_for_its_file(context):
    context.config = replace(context.config, overlap_per_file=1)
    reserve(context, 1, ["shared.py"], lane="a")
    reserve(context, 2, ["shared.py"], running=False)
    reserve(context, 3, ["other.py"], lane="b")
    reserve(context, 4, ["other.py"], running=False)
    assert not can_start(context, "c", 2)
    plan_file(context, "shared.py", 2)
    assert can_start(context, "c", 2)
    assert not can_start(context, "c", 4), "Other files keep overlap_per_file"


def test_file_limit_counts_the_candidate_and_can_lower_the_fallback(context):
    context.config = replace(context.config, overlap_per_file=3)
    reserve(context, 1, ["shared.py"], lane="a")
    reserve(context, 2, ["shared.py"], running=False)
    assert can_start(context, "c", 2)
    plan_file(context, "shared.py", 1)
    assert not can_start(context, "c", 2)


def test_glob_claim_meets_every_applicable_limit_and_the_fallback(context):
    context.config = replace(context.config, overlap_per_file=1)
    plan_file(context, "src/a.py", 5)
    planned = file_limits(context)
    assert limit_for(context, "src/a.py", planned) == 5
    assert limit_for(context, "src/b.py", planned) == 1
    assert limit_for(context, "src/*.py", planned) == 1, "A glob may also cover unplanned files"
    context.config = replace(context.config, overlap_per_file=9)
    assert limit_for(context, "src/*.py", planned) == 5


def test_fixed_mode_ignores_file_limits(context):
    context.config = replace(context.config, overlap_per_file=1, overlap_mode="fixed")
    plan_file(context, "shared.py", 4)
    reserve(context, 1, ["shared.py"], lane="a")
    reserve(context, 2, ["shared.py"], running=False)
    assert file_limits(context) == {}
    assert not can_start(context, "c", 2)


def test_file_limit_lifts_the_runner_late_file_boundary(context):
    context.config = replace(context.config, overlap_per_file=1)
    clock(context)
    plan_file(context, "shared.py", 2)
    reserve(context, 1, ["own.py"])
    reserve(context, 2, ["shared.py"], lane="peer")
    edit(context, 1, {"own.py": "own\n"}, commit=True)
    edit(context, 1, {"shared.py": "late\n"})

    def sleep(_seconds):
        raise AssertionError("A late file under its plan limit must not wait")

    context.sleep = sleep
    wait_for_overlap(attempt(context, 1))
    assert context.store.get_run(1).waiting_until is None


def test_file_limit_holds_a_late_file_at_its_lower_limit(context):
    context.config = replace(context.config, overlap_per_file=3)
    now = clock(context)
    plan_file(context, "shared.py", 1)
    reserve(context, 1, ["own.py"])
    reserve(context, 2, ["shared.py"], lane="peer")
    edit(context, 1, {"own.py": "own\n"}, commit=True)
    edit(context, 1, {"shared.py": "late\n"})
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += timedelta(seconds=5)
        context.store.finish_run(2, "merged")

    context.sleep = sleep
    wait_for_overlap(attempt(context, 1))
    assert len(waits) == 1


def test_file_entry_keeps_expiry_when_omitted_and_ends_on_its_own(context):
    expires = BASE_TIME + timedelta(hours=1)
    plan_file(context, "shared.py", 4, expires=expires)
    plan_file(context, "shared.py", 5)
    stored = json.loads((context.store.root / "plan.json").read_text())
    assert stored["files"]["shared.py"] == {"limit": 5, "why": WHY, "by": "orchestrator",
                                            "expires_at": expires.isoformat()}
    assert orch_plan.file_limits(context.store, BASE_TIME) == {"shared.py": 5}
    assert orch_plan.file_limits(context.store, expires) == {}
    plan_file(context, "new.py", 2)
    assert orch_plan.load(context.store, BASE_TIME + timedelta(days=9))["files"]["new.py"]["expires_at"] is None


def test_file_edit_leaves_other_plan_items_and_plan_expiry_leaves_files(context):
    later = BASE_TIME + timedelta(hours=2)
    orch_plan.set_plan(context.store, {"main": 2}, later, by="human")
    plan_file(context, "shared.py", 3, expires=BASE_TIME + timedelta(hours=1))
    plan = orch_plan.load(context.store, BASE_TIME)
    assert (plan["width"], plan["expires_at"], plan["authors"]["expires_at"]) == ({"main": 2}, later.isoformat(), "human")
    plan_file(context, "kept.py", 2)
    after = orch_plan.load(context.store, later)
    assert after["width"] == {} and after["files"] == {"kept.py": {
        "limit": 2, "why": WHY, "by": "orchestrator", "expires_at": None}}


def test_clear_restores_fallback_and_records_the_reason(context):
    plan_file(context, "shared.py", 4)
    plan_file(context, "shared.py", None)
    assert orch_plan.file_limits(context.store, BASE_TIME) == {}
    events = [json.loads(line) for line in (context.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["file"], e["limit"], e["why"], e["by"]) for e in events] == [
        ("plan-overlap", "shared.py", 4, WHY, "orchestrator"), ("plan-overlap", "shared.py", None, WHY, "orchestrator")]


@pytest.mark.parametrize("path", ["./src/x.py", "src/y/../x.py", "src//x.py"])
def test_equivalent_spellings_share_one_entry(context, path):
    plan_file(context, path, 2)
    assert set(orch_plan.load(context.store, BASE_TIME)["files"]) == {"src/x.py"}


@pytest.mark.parametrize("path", ["../x.py", "/etc/x.py", "src/../../x.py", ".", "src/*.py", ""])
def test_outside_or_pattern_paths_are_refused(context, path):
    with pytest.raises(orch_plan.PlanRefused):
        plan_file(context, path, 2)
    assert not (context.store.root / "plan.json").exists()


def test_case_insensitive_checkout_keeps_human_protection_across_case(context):
    plan_file(context, "src/X.py", 1, by="human", case_sensitive=False)
    before = (context.store.root / "plan.json").read_bytes()
    with pytest.raises(orch_plan.PlanRefused, match="human"):
        plan_file(context, "src/x.py", 5, case_sensitive=False)
    assert (context.store.root / "plan.json").read_bytes() == before


def test_overlap_needs_a_reason(context):
    with pytest.raises(orch_plan.PlanRefused, match="--why"):
        orch_plan.set_files(context.store, {"a.py": 2}, " ", None, "orchestrator")
