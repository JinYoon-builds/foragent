"""Completed-order lane crossings appear in the architect's public facts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

from foragent.state import StateStore
from test_one_entrance_seat import invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


def test_finished_order_facts_name_crossing_issues_and_lane_pairs_without_double_counting(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    second = deepcopy(case.plan["issues"][0])
    second.update(key="display", title="Show locale", lane="view")
    second["acceptance_files"] = {"tests/acceptance/test_pm_display_fixture.py": "def test_display():\n    assert True\n"}
    second["done_when"] = ["uv run pytest -q tests/acceptance/test_pm_display_fixture.py"]
    case.plan["issues"].append(second)
    case.path.write_text(json.dumps(case.plan))
    # Publication through the public CLI gives the real parent/child record format.
    assert invoke(capsys, "order", "publish", case.path).code == 0
    parent = next(i for i in case.tracker.list_issues() if i.title == "Offline work")
    children = case.tracker.sub_issues(parent.number)
    source = next(i for i in children if i.title == "Keep locale")
    local = next(i for i in children if i.title == "Show locale")
    clock = [datetime.now(UTC)]
    writer = StateStore(case.store.root, now=lambda: clock[0])
    for issue, lane, paths in ((source, "api", ["api/service.py", "view/panel.py"]),
                                (local, "view", ["view/panel.py"])):
        writer.start_run(issue.number, branch=f"foragent/issue-{issue.number}", worktree=case.root / str(issue.number), lane=lane)
        writer.update_run(issue.number, changed_files=paths)
        writer.record_merge(issue.number, clock[0])
        writer.finish_run(issue.number, outcome="merged")
        case.tracker.close_issue(issue.number)
        writer.record_issue(case.tracker.get_issue(issue.number), case.values["lanes"])
        clock[0] += timedelta(seconds=1)
    case.tracker.close_issue(parent.number)
    writer.record_issue(case.tracker.get_issue(parent.number), case.values["lanes"])
    for _ in range(2):
        result = invoke(capsys, "orch", "facts", "--json")
        assert result.code == 0, result.text
        facts = json.loads(result.out)
        rows = facts.get("cross_lane_orders")
        assert isinstance(rows, list), "Architect facts must include completed orders' cross-lane evidence"
        row, = [r for r in rows if r["order"] == parent.number]
        assert row["issues"] == [{"number": source.number, "lane": "api", "other_lanes": ["view"]}]
        assert row["lane_pairs"] == [["api", "view"]], "Same-lane work is not a crossing; repeated reads are not new facts"
