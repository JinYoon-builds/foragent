"""Acceptance test for #2449 statement 2 (PM q65 option 1).

While its publication is in preparation, the orchestrator request for a returned
publication shows as open, whatever was recorded on it. It closes when the publication
is published again or superseded by a new attempt, and the same request reopens if
that attempt is returned again (withdrawal is out of scope, so it is not tested here).

Reuses the scenario of the locked tests/acceptance/test_returned_publication_owner.py
(real merge owner, publication queue, preparation record and CLI; fake Git, GitHub and
measured test runs). No network, model or signal.
"""

from datetime import timedelta

from test_order_publications import case, submit  # noqa: F401  (fixture)
from test_returned_publication_owner import (  # noqa: F401  (fixture)
    LONG_REASON, SECOND_REASON, carries, inbox, order_requests, owner, owner_passes, red, reprepare,
    restart, returned, run_cli, statuses)

OPEN = {"new", "open"}


def only_request(case, capsys, *options):
    found = order_requests(case, inbox(case, capsys, *options))
    assert len(found) == 1, (
        f"Exactly one orchestrator request for the order, not {[(r['id'], r.get('status')) for r in found]}")
    return found[0]


def record(case, capsys, request_id, status):
    """The orchestrator records a status; whether the CLI accepts it is not the point."""
    run_cli(case, capsys, "orch", "request-status", request_id, status,
            "--note", f"recorded {status}", "--answer", "republish later")


def test_recorded_status_never_hides_the_request_across_passes_and_a_restart(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    request = only_request(case, capsys)
    for status in ("processing", "done", "processing", "done"):
        record(case, capsys, request["id"], status)
        case.clock[0] += timedelta(minutes=7)
        owner_passes(case)
        restart(case)
        assert statuses(case)[-1] == "preparation"
        shown = only_request(case, capsys, "--pending")
        assert shown["id"] == request["id"], "The open work is the request the return opened"
        assert shown["status"] in OPEN, (
            f"After recording {status!r}, a request whose publication is in preparation shows open, "
            f"not {shown['status']!r}")


def test_a_new_attempt_closes_the_request_and_its_return_reopens_the_same_one(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    first = only_request(case, capsys)
    record(case, capsys, first["id"], "processing")
    # A new attempt supersedes the returned publication: the request closes at once.
    reprepare(case)
    case.verdict.clear()
    case.verdict.update(status="hold", reason=SECOND_REASON, exit_code=2,
                        collected_node_ids=None, failing_node_ids=None)
    submit(case)
    assert order_requests(case, inbox(case, capsys, "--pending")) == [], (
        "A new attempt supersedes the returned publication and closes its request")
    closed = {row["id"]: row for row in inbox(case, capsys)}
    assert closed[first["id"]]["status"] == "done", "The superseded publication's request is closed, not open"
    # The new attempt is returned again: the same request reopens with the new reason.
    owner_passes(case)
    assert statuses(case)[-1] == "preparation"
    reopened = only_request(case, capsys, "--pending")
    assert reopened["id"] == first["id"], "A second return reopens the same request, not a new one"
    assert reopened["status"] in OPEN, f"The reopened request shows open, not {reopened['status']!r}"
    assert carries(reopened, SECOND_REASON), "The reopened request carries the latest reason in full"
    every = order_requests(case, inbox(case, capsys))
    assert [row["id"] for row in every] == [first["id"]], "No second request exists, open or closed"


def test_a_request_recorded_done_reopens_on_a_later_return_and_closes_when_published(owner, capsys):
    """Full lifecycle: return -> recorded done -> new attempt returned (same request reopens)
    -> published again (that request closes and stays closed, no other request)."""
    case = owner
    returned(case, LONG_REASON)
    first = only_request(case, capsys)
    record(case, capsys, first["id"], "done")
    case.clock[0] += timedelta(minutes=20)
    reprepare(case)
    returned(case, SECOND_REASON, collection=True)
    restart(case)
    reopened = only_request(case, capsys, "--pending")
    assert reopened["id"] == first["id"], "The same request reopens after a later return"
    assert reopened["status"] in OPEN, f"It shows open, not {reopened['status']!r}"
    # The orchestrator corrects it and the publication is published again.
    reprepare(case)
    red(case)
    submit(case)
    owner_passes(case)
    assert statuses(case)[-1] == "published"
    for _ in range(2):
        restart(case)
        assert order_requests(case, inbox(case, capsys, "--pending")) == [], (
            "A published publication leaves no open request, even after more passes")
        rows = {row["id"]: row for row in inbox(case, capsys)}
        assert rows[first["id"]]["status"] == "done", "The request is closed once published again"
    assert [row["id"] for row in order_requests(case, inbox(case, capsys))] == [first["id"]], (
        "Publishing again opens no other request")
