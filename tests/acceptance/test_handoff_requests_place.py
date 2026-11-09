"""Acceptance: an accepted request stays on the shelf until its effect is seen
(order 2026-11-09 invoice step 1, issue handoff-requests-place; Jin r1322, architect r1321).

Public entry points only: the gate tick (`scheduler.tick`, what `fa gate tick` runs),
`fa orch request --from <role>` (a role proposal), `fa orch role-decision ID accepted|rejected
--answer TEXT`, `fa orch inbox --json`, `fa brief --json` (``shelves.requests.count``) and the
orch journal (`<state>/orch-events.jsonl`, ``kind: shelf-cleared``, ``place: requests``).
The shelf world is the locked `shelf` fixture of test_shelf_requests_notes_lessons (FakeTracker,
local state store, fake Popen and process table; no network, no real signal).

Contract choices these tests lock (the issue leaves the form open):

- The effect reference is recorded in the decision's ``--answer`` text: a bare ``#N`` of an issue
  published on the tracker, or a main-write request id (``main-writes/<id>.json`` in the state
  store) whose receipt (``Foragent-Main-Write: <id>`` trailer) is on origin's main.
- A request's age on the shelf is counted from when it was filed, as today; every scenario files
  and decides within one minute, so counting from the decision instead gives the same results.
- A shelf alarm for the place is one request ``from: gate``, ``kind: shelf-alarm``,
  ``place: requests``; its ``items`` name the request ids that were on the place when it opened.
"""

from datetime import timedelta
import json

import pytest

from foragent import main_write_requests as writes
from test_board_status_groups import offline_github  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_shelf_requests_notes_lessons import (  # noqa: F401
    brief, cli_exit, inbox, isolated_home, later, request, shelf, tick,
)

PLACE = "requests"
TRAILER = "Foragent-Main-Write"


# ---------------------------------------------------------------- helpers


def decide(case, request_id, decision, answer):
    case.capsys.readouterr()
    code = cli_exit(case.main, ["orch", "role-decision", request_id, decision, "--answer", answer,
                                "--repo-root", str(case.root)])
    output = case.capsys.readouterr()
    assert code == 0, f"fa orch role-decision {request_id} {decision} failed: {output.err}"
    (row,) = [row for row in inbox(case) if row["id"] == request_id]
    assert row["status"] == "done" and row.get("accepted") is (decision == "accepted"), row
    return row


def place_alarms(case):
    """Every shelf alarm of the requests place, open or done, oldest first."""
    rows = [row for row in inbox(case) if row.get("from") == "gate" and row.get("kind") == "shelf-alarm"
            and row.get("place") == PLACE]
    return sorted(rows, key=lambda row: row["created_at"])


def open_place_alarms(case):
    return [row for row in place_alarms(case) if row["status"] != "done"]


def cleared(case):
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("kind") == "shelf-cleared" and row.get("place") == PLACE]


def place_count(case):
    return brief(case)["shelves"][PLACE]["count"]


def items(alarm):
    return {str(item) for item in alarm.get("items") or []}


def settle(case):
    """A first tick, then close anything the gate filed by itself so only the test's requests age."""
    tick(case)
    root = ["--repo-root", str(case.root)]
    for row in inbox(case, pending=True):
        case.capsys.readouterr()
        assert cli_exit(case.main, ["orch", "request-status", row["id"], "done", "--answer",
                                    "Not part of this test", *root]) == 0, row
    later(case, minutes=1)
    tick(case)
    assert not open_place_alarms(case) and not cleared(case)
    return place_count(case)


def file_old_proposal(case, text, hours=25):
    """A role proposal filed ``hours`` ago and still open; the clock returns to now."""
    now = case.clock[0]
    case.clock[0] = now - timedelta(hours=hours)
    row = request(case, text, source="architect")
    case.clock[0] = now
    later(case, minutes=0)
    return row


def file_and_decide(case, text, decision, answer):
    row = request(case, text, source="architect")
    later(case, minutes=1)
    return decide(case, row["id"], decision, answer)


def open_alarm_for(case, request_id):
    """Tick with an over-age open request: exactly one open alarm naming it."""
    tick(case)
    alarms = open_place_alarms(case)
    assert len(alarms) == 1 and request_id in items(alarms[0]), (
        f"A request open for 25 hours must raise one requests alarm naming it: {place_alarms(case)}")
    return alarms[0]


def publish_issue(case, number):
    case.tracker.add_issue(number, f"Published effect {number}",
                           "<!-- foragent:v1 -->\n## Goal\nThe proposal's effect.\n"
                           "## Out of scope\nOther work.\n## Done when\n```sh\ntrue\n```\n")


def queue_main_write(case, name, content):
    return writes.submit(case.store, "main", f"Docs edit {name}", "role docs",
                         {"kind": "files", "files": {name: content}})


def land_main_write(case, row, name, content):
    """The receipt lands on origin's main (and the local tracking ref); the queue row is done."""
    from conftest import git_command

    case.history.tip = git_command(case.history.origin, "rev-parse", "refs/heads/main")
    case.history.update({name: content}, at=case.clock[0],
                        message=f"Docs edit {name}\n\n{TRAILER}: {row['id']}")
    git_command(case.root, "fetch", "origin")
    landed = git_command(case.history.origin, "rev-parse", "refs/heads/main")
    path = case.store.root / "main-writes" / f"{row['id']}.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.update(status="done", landed=landed)
    path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    return landed


# ---------------------------------------------------------------- statement 1


def test_accepted_request_without_reference_stays_and_raises_one_alarm_after_its_age_limit(shelf):
    case = shelf
    baseline = settle(case)
    row = file_and_decide(case, "Proposal: split the merge owner module Ꮚ1", "accepted",
                          "Agreed; we will order it later.")
    tick(case)
    assert place_count(case) == baseline + 1, (
        "A request accepted without an effect reference must stay in the shelf place 'requests'")
    assert not place_alarms(case), "Inside its 24-hour age limit the accepted request raises no alarm"

    later(case, hours=23)
    tick(case)
    assert not place_alarms(case), "23 hours after filing it is still inside the age limit"

    later(case, hours=2)  # Now 25 hours after filing and decision.
    tick(case)
    alarms = place_alarms(case)
    assert len(alarms) == 1, (
        f"An accepted request with no effect reference past 24 hours must raise one shelf alarm: {alarms}")
    assert alarms[0]["status"] != "done" and row["id"] in items(alarms[0]), alarms[0]
    assert place_count(case) == baseline + 1

    for _ in range(3):
        later(case, minutes=5)
        tick(case)
    assert len(place_alarms(case)) == 1, "The same episode must not raise a second alarm"
    assert len(open_place_alarms(case)) == 1, "The alarm stays open while the request waits for its effect"
    assert not cleared(case), "Nothing left the place, so no shelf-cleared is logged"


def test_accepted_request_without_reference_holds_the_open_alarm(shelf):
    """An over-age request accepted without a reference does not end its alarm episode."""
    case = shelf
    baseline = settle(case)
    old = file_old_proposal(case, "Proposal: move the docs map Ꮚ2")
    open_alarm_for(case, old["id"])
    later(case, minutes=1)
    decide(case, old["id"], "accepted", "Agreed, a good idea.")
    for _ in range(2):
        later(case, minutes=5)
        tick(case)
    assert place_count(case) == baseline + 1, "The accepted request is still on the place"
    assert len(open_place_alarms(case)) == 1 and len(place_alarms(case)) == 1, place_alarms(case)
    assert not cleared(case), "No effect reference, so the place is not cleared"


# ---------------------------------------------------------------- statement 2


def test_request_accepted_with_published_issue_leaves_and_is_logged_as_cleared(shelf):
    case = shelf
    publish_issue(case, 42)  # Published before the first tick, so the gate's listing has seen it.
    baseline = settle(case)
    old = file_old_proposal(case, "Proposal: one owner for the request box Ꮚ3")
    alarm = open_alarm_for(case, old["id"])
    later(case, minutes=1)
    decide(case, old["id"], "accepted", "Published as #42.")
    # #99 was never published, so it is no effect the gate can see.
    young = file_and_decide(case, "Proposal: a second, unordered idea Ꮚ4", "accepted",
                            "Agreed; it will be ordered as #99.")
    later(case, minutes=1)
    tick(case)

    assert [row["id"] for row in place_alarms(case) if row["status"] == "done"] == [alarm["id"]], (
        f"Once its only over-age request names a published issue the alarm is done: {place_alarms(case)}")
    assert len(cleared(case)) == 1, f"Leaving the place is logged as one shelf-cleared: {cleared(case)}"
    assert place_count(case) == baseline + 1, "Only the request without a reference is left on the place"

    later(case, minutes=5)
    tick(case)
    assert len(cleared(case)) == 1, "A later tick logs no second shelf-cleared"

    later(case, hours=25)  # The unreferenced request is now past its age limit; the other is gone.
    tick(case)
    alarms = open_place_alarms(case)
    assert len(alarms) == 1, f"The request accepted without a reference raises a new alarm: {place_alarms(case)}"
    assert young["id"] in items(alarms[0]) and old["id"] not in items(alarms[0]), alarms[0]


def test_request_accepted_with_landed_main_write_leaves_and_pending_one_stays(shelf):
    case = shelf
    baseline = settle(case)
    landed = queue_main_write(case, "docs/landed.md", "Landed edit\n")
    waiting = queue_main_write(case, "docs/waiting.md", "Waiting edit\n")
    land_main_write(case, landed, "docs/landed.md", "Landed edit\n")

    old = file_old_proposal(case, "Proposal: rewrite the mechanisms page Ꮚ5")
    alarm = open_alarm_for(case, old["id"])
    later(case, minutes=1)
    decide(case, old["id"], "accepted", f"Landed through main write {landed['id']}.")
    young = file_and_decide(case, "Proposal: another docs edit Ꮚ6", "accepted",
                            f"Queued as main write {waiting['id']}.")
    later(case, minutes=1)
    tick(case)

    assert [row["id"] for row in place_alarms(case) if row["status"] == "done"] == [alarm["id"]], (
        "A request whose main-write receipt is on main leaves, so its alarm is done")
    assert len(cleared(case)) == 1, cleared(case)
    assert place_count(case) == baseline + 1, (
        "The request whose main write has no receipt on main stays on the place")

    later(case, hours=25)
    tick(case)
    alarms = open_place_alarms(case)
    assert len(alarms) == 1, (
        f"A main-write id without a receipt on main is no effect yet; past its age it alarms: {place_alarms(case)}")
    assert young["id"] in items(alarms[0]) and old["id"] not in items(alarms[0]), alarms[0]

    land_main_write(case, waiting, "docs/waiting.md", "Waiting edit\n")
    later(case, minutes=5)
    tick(case)
    assert not open_place_alarms(case), "Once its receipt is on main the request leaves and the alarm ends"
    assert len(cleared(case)) == 2, f"The second departure is logged as cleared once: {cleared(case)}"
    assert place_count(case) == baseline


# ---------------------------------------------------------------- statement 3


def test_rejected_request_leaves_the_place_at_once(shelf):
    case = shelf
    baseline = settle(case)
    old = file_old_proposal(case, "Proposal: a role nobody needs Ꮚ7")
    alarm = open_alarm_for(case, old["id"])
    later(case, minutes=1)
    decide(case, old["id"], "rejected", "No: the existing lane already covers it.")
    assert place_count(case) == baseline, "A rejected request is off the place at once, before any tick"
    young = file_and_decide(case, "Proposal: an accepted idea with no effect yet Ꮚ8", "accepted",
                            "Agreed; not ordered yet.")
    tick(case)

    assert [row["id"] for row in place_alarms(case) if row["status"] == "done"] == [alarm["id"]], (
        "The rejected request left on the next tick, so its alarm is done")
    assert len(cleared(case)) == 1, cleared(case)
    assert place_count(case) == baseline + 1, "The accepted one without a reference stays"

    later(case, hours=25)
    tick(case)
    alarms = open_place_alarms(case)
    assert len(alarms) == 1, f"Only the accepted request without a reference alarms later: {place_alarms(case)}"
    assert young["id"] in items(alarms[0]) and old["id"] not in items(alarms[0]), alarms[0]


@pytest.mark.parametrize("answer", ["Rejected; see #42 for the reason.", "No."])
def test_rejection_leaves_whatever_its_answer_names(shelf, answer):
    """A rejection never waits for an effect, even when its answer happens to name a reference."""
    case = shelf
    baseline = settle(case)
    accepted = file_and_decide(case, "Proposal: kept idea Ꮚ9", "accepted", "Agreed; not ordered yet.")
    rejected = file_and_decide(case, "Proposal: dropped idea Ꮚ10", "rejected", answer)
    tick(case)
    assert place_count(case) == baseline + 1, "Only the accepted request without a reference stays"
    later(case, hours=25)
    tick(case)
    alarms = open_place_alarms(case)
    assert len(alarms) == 1 and items(alarms[0]) >= {accepted["id"]}, place_alarms(case)
    assert rejected["id"] not in items(alarms[0]), alarms[0]
