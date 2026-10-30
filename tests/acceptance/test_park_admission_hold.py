"""Historical hold-test name retained: park now removes GitHub approval."""

import json

import pytest

from foragent import capacity, scheduler
from test_board_status_groups import offline_github  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


@pytest.mark.parametrize("for_person", [False, True])
def test_park_holds_approved_ready_work_until_unpark_despite_label_changes(
    git_history, tmp_path, monkeypatch, capsys, for_person,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], language="en", test_command="true")
    case.store.finish_run(1, "error")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    assert scheduler.tick(tick_context(case, start_runs=False)).skipped is None
    assert {"approved", "ready"} <= case.tracker.get_issue(1).labels
    main = bind_cli(case, monkeypatch)
    argv = ["orch", "park", "1", "--why", "Wait for the scope decision"]
    assert cli_exit(main, argv + (["--for-person"] if for_person else [])) == 0
    labels = case.tracker.get_issue(1).labels
    assert "approved" not in labels, "Park must revoke admission at its existing label source"
    assert ("waiting-person" in labels) is for_person
    launched = []
    context = tick_context(case)
    context.launcher = lambda n: launched.append(n) or capacity.os.getpid()
    assert scheduler.tick(context).skipped is None
    assert launched == [] and case.store.active_runs() == []
    # Cosmetic relabel and unpark do not grant approval.
    case.tracker.add_labels(1, ["waiting-person"])
    assert cli_exit(main, ["orch", "unpark", "1"]) == 0
    assert not case.tracker.get_issue(1).labels & {"approved", "waiting-person"}
    assert scheduler.tick(context).skipped is None
    assert launched == []
    # Model only the valid approval edge produced by fa order publish; publication
    # idempotency is already shipped and is not implemented by this issue.
    from datetime import timedelta
    case.clock[0] += timedelta(seconds=1)
    case.tracker.record_label(1, "approved", "human")
    assert scheduler.tick(context).skipped is None
    assert launched == [1]
    assert scheduler.tick(context).skipped is None
    assert launched == [1]
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert len([r for r in events if r["kind"] == "parked"]) == 1
    assert len([r for r in events if r["kind"] == "unparked"]) == 1
