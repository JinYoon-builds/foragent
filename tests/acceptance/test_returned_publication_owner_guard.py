"""Regression guard for #2449 (outside Done when): a second return keeps one request with the new reason."""

from datetime import timedelta

from test_order_publications import case  # noqa: F401 (fixture)
from test_returned_publication_owner import LONG_REASON, SECOND_REASON, carries, inbox, order_requests, owner, owner_passes, reprepare, returned, statuses  # noqa: F401 (fixtures and helpers)


def test_a_second_return_keeps_one_request_that_carries_the_new_reason_in_full(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    case.clock[0] += timedelta(minutes=30)
    reprepare(case)
    returned(case, SECOND_REASON, collection=True)
    assert statuses(case)[-1] == "preparation"
    owner_passes(case)
    found = order_requests(case, inbox(case, capsys, "--pending"))
    assert len(found) == 1, (
        f"A publication returned twice still has exactly one orchestrator request, not {len(found)}")
    assert carries(found[0], SECOND_REASON), "The request must carry the full reason of the latest return"
    assert found[0]["status"] != "done", "The request stays open while the publication is in preparation"
