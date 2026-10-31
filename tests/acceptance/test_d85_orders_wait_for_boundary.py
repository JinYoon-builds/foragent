"""D85 piece 2. Locked acceptance; queue orders without needing flush."""

from test_d85_batched_publication import (
    BOUNDARY, assert_requirements, batch, call, children, pending, prepare, shared_files,
)
from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def test_approved_ordinary_order_waits_durably_with_no_commit_or_issues(batch, capsys):
    case = batch
    value, path = prepare(case, capsys, "ordinary")
    before = shared_files(case)
    message = call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    assert "waiting" in message.lower() and BOUNDARY in message, message
    assert shared_files(case) == before
    assert case.events == [] and case.tracker.list_issues() == []
    path.unlink()  # Waiting retains the order, not just the caller's pathname.
    for _ in range(2):
        rows = pending(case, capsys)["orders"]
        row, = [row for row in rows if row["id"] == value["id"]]
        assert row["status"] == "waiting" and row["boundary"] == BOUNDARY, row
    assert case.events == [] and case.tracker.list_issues() == []


def test_agent_judged_urgent_order_publishes_without_draining_waiting_work(batch, capsys):
    case = batch
    ordinary, path = prepare(case, capsys, "ordinary")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY)
    urgent, path = prepare(case, capsys, "urgent")
    call(case, capsys, "publish", path, "--approve", "--defer", BOUNDARY, "--urgent")
    assert_requirements(case, urgent, approved=True)
    assert children(case, ordinary) == {}
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len(case.tracker.list_issues()) == 3
    row, = [row for row in pending(case, capsys)["orders"] if row["id"] == ordinary["id"]]
    assert row["status"] == "waiting"


def test_waiting_an_unapproved_order_does_not_publish_or_approve_it(batch, capsys):
    case = batch
    value, path = prepare(case, capsys, "unapproved")
    before = shared_files(case)
    call(case, capsys, "publish", path, "--defer", BOUNDARY)
    for _ in range(3):
        row, = [row for row in pending(case, capsys)["orders"] if row["id"] == value["id"]]
        assert row["status"] == "waiting"
    assert shared_files(case) == before
    assert case.events == [] and case.tracker.list_issues() == []


def test_waiting_cannot_bypass_clean_preread(batch, capsys):
    case = batch
    prepare(case, capsys, "unchecked", clean=False)
    code = cli_exit(case.main, ["order", "publish", str(case.path), "--approve", "--defer", BOUNDARY])
    output = capsys.readouterr()
    assert code != 0 and "preread" in (output.out + output.err).lower(), output
    assert case.events == [] and case.tracker.list_issues() == []
