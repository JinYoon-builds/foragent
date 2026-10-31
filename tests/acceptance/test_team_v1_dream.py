"""A lane has one designated dream writer; memory splitting waits on overlap."""
from datetime import timedelta

from foragent.orch_dream import dream_due
from foragent.scheduler import tick
from test_architect_project_schedule import order_children
from test_lane_dream import dream_case, dream_finish
from test_orch_review_handoff import tick_context
from team_v1_support import factory, legacy, person


def test_only_designated_person_can_offer_lane_dream(git_history, tmp_path, monkeypatch):
    case = dream_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    case.config.lanes["orch"]["dream_owner"] = "alice"
    dream_finish(case, 10, "orch")
    order_children(case, 900, [10])
    person(case.tracker, monkeypatch, "bob")
    assert "orch" not in dream_due(case.store, case.config, case.tracker, case.clock[0])
    person(case.tracker, monkeypatch, "alice")
    assert dream_due(case.store, case.config, case.tracker, case.clock[0]).count("orch") == 1


def test_foreign_open_memory_split_blocks_dream_for_matching_files(git_history, tmp_path, monkeypatch):
    case = dream_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    case.config.lanes["orch"]["dream_owner"] = "alice"
    person(case.tracker, monkeypatch, "alice")
    dream_finish(case, 10, "orch")
    order_children(case, 900, [10])
    # A separate lane-memory/night-order issue can have another lane/orderer.
    # Read declared Files; don't infer intent from its title or keywords.
    body = "<!-- foragent:v1 -->\n## Goal\nSplit memory.\n## Files\n- .claude/agent-memory/fa-orch/MEMORY.md\n- .claude/agent-memory/fa-orch/MEMORY.new.md\n## Out of scope\nRest.\n## Done when\n```sh\ntrue\n```\n"
    case.tracker.add_issue(20, "Night order", body, labels=["by:bob", "lane:view", "approved"])
    assert "orch" not in dream_due(case.store, case.config, case.tracker, case.clock[0])
    case.tracker.close_issue(20)
    assert "orch" in dream_due(case.store, case.config, case.tracker, case.clock[0])
