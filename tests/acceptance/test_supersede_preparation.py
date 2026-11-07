"""Acceptance test for supersede-preparation. Locked by Foragent; do not edit.

A newer publication request for an order supersedes every older request of that order
that is pending or was returned to preparation, and names the newer request as the reason.
A superseded request is no longer open work: it leaves the orchestrator's waiting list and
its orchestrator inbox request is closed. Keeping older pending requests superseded and other
orders untouched is guarded by PR CI and the current tests; this file only checks that an
in-preparation request of another order is not swept up with the order being republished.

The real admission (order.publish), red check (check_pending), owner pass (apply_next) and
inbox listing (the reader behind `fa orch inbox --pending`) are used with the fake Git,
tracker and measured tests of tests/test_order_publications.py.
"""

from copy import deepcopy

from foragent import order_prepare, order_publications, orch_inbox
from foragent.scheduler import TickReport
from test_order_publications import case, review_plan, submit  # noqa: F401


def return_to_preparation(case):
    """Publish the order and let the red check return it to preparation with a PM notice."""
    submit(case)
    case.git.remote.head = "main-2"
    case.verdict.update(status="hold", reason="Tests passed on current main", exit_code=0)
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["status"] == "preparation", row
    return row


def reprepare_and_revise(case):
    """A fresh preparation of a revised order, as the orchestrator re-authors it."""
    case.verdict.clear()
    case.verdict.update(status="reviewed", reason="Still red on current main")
    case.plan["summary"] = "Revised order text"
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"].update(status="reviewed", phase="reviewing_tests", reason="Red")
    order_prepare._write(path, record)
    review_plan(case)


def inbox_request_for(case, identity):
    rows = [row for row in orch_inbox.requests(case.store) if row.get("publication") == identity]
    assert len(rows) <= 1, f"One inbox request per publication, got {rows}"
    return rows[0] if rows else None


def open_inbox_ids(case):
    return {row["id"] for row in orch_inbox.requests(case.store, pending=True)}


def legacy_copy(row, identity, sequence, *, status, plan_id=None, pm_request=None):
    copy = deepcopy(row)
    copy.update(id=identity, sequence=sequence, status=status, fingerprint=f"older-{identity}",
                reason=f"Legacy {status} reason")
    copy.pop("pm_request", None)
    if pm_request is not None:
        copy["pm_request"] = pm_request
    if plan_id is not None:
        copy["plan"]["id"] = plan_id
    return copy


def test_newer_request_supersedes_older_request_in_preparation_and_closes_its_inbox_request(case):
    returned = return_to_preparation(case)
    notice = inbox_request_for(case, returned["id"])
    assert notice is not None and notice["id"] in open_inbox_ids(case), (
        "Precondition: the returned publication has one open orchestrator inbox request")

    reprepare_and_revise(case)
    newer = submit(case).publication_request
    assert newer and newer != returned["id"]

    old, = [row for row in order_publications.requests(case.store) if row["id"] == returned["id"]]
    assert old["status"] == "superseded", (
        f"The older request in preparation must become superseded, not stay {old['status']!r}")
    assert newer in (old["reason"] or ""), "The reason must name the newer publication request"
    assert case.plan["id"] in old["reason"], "The reason must name the order"

    closed = inbox_request_for(case, returned["id"])
    assert closed is not None, "The superseded request's inbox request stays on record"
    assert closed["id"] not in open_inbox_ids(case), (
        "The superseded request's orchestrator inbox request must be closed, not left open")
    assert closed["status"] == "done", closed

    # It no longer counts as open work: an owner pass neither reopens nor re-notifies it.
    order_publications.apply_next(case.ctx, TickReport())
    old, = [row for row in order_publications.requests(case.store) if row["id"] == returned["id"]]
    assert old["status"] == "superseded" and newer in old["reason"]
    assert inbox_request_for(case, returned["id"])["id"] not in open_inbox_ids(case)
    assert [row["id"] for row in order_publications.status(case.store)] == [newer], (
        "Only the newer request is waiting publication work")


def test_every_older_request_in_preparation_of_the_order_is_superseded_once(case):
    returned = return_to_preparation(case)
    notice = inbox_request_for(case, returned["id"])
    first = returned["sequence"]

    # A stuck older request in preparation that was never notified, and a request of
    # another order in preparation with its own inbox request.
    stuck = legacy_copy(returned, "stuck-preparation", first - 2, status="preparation")
    other_notice = orch_inbox.add_request(case.store, "gate", "Order Other returned to preparation: red",
                                          extra={"publication": "other-preparation"})
    other_prep = legacy_copy(returned, "other-preparation", first - 3, status="preparation",
                             plan_id="another-plan", pm_request=other_notice["id"])
    for row in (stuck, other_prep):
        order_publications._save(case.store, row)
    others_before = {other_prep["id"]: deepcopy(other_prep)}

    reprepare_and_revise(case)
    newer = submit(case).publication_request
    rows = {row["id"]: row for row in order_publications.requests(case.store)}

    for identity in (returned["id"], stuck["id"]):
        assert rows[identity]["status"] == "superseded", (
            f"Older request {identity} of the same order must be superseded, not {rows[identity]['status']!r}")
        assert newer in rows[identity]["reason"], f"{identity} must name the newer request {newer}"
    assert rows[newer]["status"] == "pending"

    for identity, before in others_before.items():
        assert rows[identity] == before, f"Request {identity} of another order is not superseded"
    assert other_notice["id"] in open_inbox_ids(case), "Another order's inbox request stays open"
    assert notice["id"] not in open_inbox_ids(case)

    # Republishing the same revision again does not supersede twice or open new inbox work.
    assert submit(case).publication_request == newer
    again = {row["id"]: row for row in order_publications.requests(case.store)}
    assert again[newer]["status"] == "pending"
    for identity in (returned["id"], stuck["id"]):
        assert again[identity] == rows[identity], f"{identity} must be superseded once, not rewritten"
    order_publications.apply_next(case.ctx, TickReport())
    assert inbox_request_for(case, stuck["id"]) is None or (
        inbox_request_for(case, stuck["id"])["id"] not in open_inbox_ids(case)), (
        "A superseded request must not get a new open inbox request")
    assert len([row for row in orch_inbox.requests(case.store)
                if row.get("publication") == returned["id"]]) == 1
