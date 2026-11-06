"""Publication recovery reconstructs each member's local lane environment."""

from dataclasses import replace
import os

import pytest

from test_order_publications import case, three_requests  # noqa: F401
from foragent import order, order_flush_journal, order_prepare_check, order_prepare_cycle
from foragent import order_prepare_setup, order_publications
from foragent.scheduler import TickReport


@pytest.mark.parametrize("legacy_intent", [False, True])
def test_lost_push_restores_each_members_lane_executable(case, monkeypatch, legacy_intent):
    rows = three_requests(case)
    lanes = {f"lane-{index}": {"paths": [f"src/{index}/**"], "agent": f"fa-lane-{index}",
                              "skills": [f"skill-{index}"]}
             for index in range(len(rows))}
    case.config = replace(case.config, lanes=lanes)
    case.ctx.config = case.config
    for index in range(len(rows)):
        lane, skill = f"lane-{index}", f"skill-{index}"
        agent = case.root / f".claude/agents/fa-{lane}.md"
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(f"Use {skill}.\n")
        executable = case.root / f".claude/skills/{skill}/check"
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\nexec python -m pytest "$@"\n')
        executable.chmod(0o755)
    for index, row in enumerate(rows):
        lane, skill = f"lane-{index}", f"skill-{index}"
        item = row["plan"]["issues"][0]
        command = f"./.claude/skills/{skill}/check {next(iter(item['acceptance_files']))}"
        item.update(lane=lane, done_when=[command])
        entry = row["prepared"]["new"]
        entry.update(done_when=[command],
                     cycle=order_prepare_cycle.identity(row["plan"], item, case.config, case.root, row["files"]),
                     published=order_prepare_setup.published_digest(row["plan"], case.root, case.config, row["files"]))
        order_publications._save(case.store, row)

    order_publications.check_pending(case.ctx, TickReport())
    checked = order_publications.requests(case.store)
    assert all(row["status"] == "pending" for row in checked), [(row["id"], row["reason"]) for row in checked]
    monkeypatch.setattr(order_prepare_check, "run", lambda *args, **kwargs: pytest.fail("Owner ran Done when"))
    case.git.remote.lost = True
    order_publications.apply_next(case.ctx, TickReport())
    assert all(row["status"] == "applying" for row in order_publications.requests(case.store))
    assert not case.tracker.list_issues()
    if legacy_intent:
        (case.store.root / order_flush_journal.JOURNAL).write_text("")

    resumed = []
    check = order._publication_checks

    def publication_checks(plan, root, *args, **kwargs):
        if kwargs.get("landed"):
            item = plan["issues"][0]
            own_skill = lanes[item["lane"]]["skills"][0]
            assert os.access(root / f".claude/skills/{own_skill}/check", os.X_OK)
            assert not any((root / f".claude/skills/{skill}/check").exists()
                           for lane in lanes.values() for skill in lane["skills"] if skill != own_skill)
            resumed.append(plan["id"])
        return check(plan, root, *args, **kwargs)

    monkeypatch.setattr(order, "_publication_checks", publication_checks)
    order_publications.apply_next(case.ctx, TickReport())
    assert len(resumed) == len(rows)
    assert all(row["status"] == "published" for row in order_publications.requests(case.store))
    assert len(case.tracker.list_issues()) == 2 * len(rows)
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1
    assert not order_publications.apply_next(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == 2 * len(rows)
