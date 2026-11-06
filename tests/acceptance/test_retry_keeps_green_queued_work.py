"""Retry preserves green merge-owner work unless its discard is explicit.

The real runner admits a verified PR through the offline merge-owner world;
the public CLI must protect that work before closing it or consuming advice.
"""

import pytest

from foragent import cli, merge_owner
from foragent.run_previous import add_hint, hint_path, retry_marker
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from test_orch_review_handoff import bind_cli


N = 5


@pytest.fixture
def green_queued_case(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [N])
    pr = case.tracker.get_pr(support.pr_for(case, N).number)
    (entry,) = merge_owner.admissions(case.store).values()
    assert case.config.merge_owner
    assert pr.state == "open" and pr.head_sha
    assert entry["n"] == N and entry["publication"]["pr"] == pr.number
    assert entry["head_sha"] == pr.head_sha
    assert case.tracker.pr_checks(pr.number) == "pass"

    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "RunContext", lambda **kwargs: case.ctx)
    return case, main, pr


def test_retry_refuses_green_merge_queued_work(green_queued_case, capsys):
    case, main, pr = green_queued_case
    queued = merge_owner.admissions(case.store)
    hint = "Keep the existing implementation and its verified head."
    add_hint(case.store, N, hint)
    capsys.readouterr()

    result = main(["orch", "retry", str(N)])
    captured = capsys.readouterr()
    message = captured.out + captured.err

    assert result != 0, "Retry must refuse to discard green merge-queued work without --discard-queued"
    assert case.tracker.get_pr(pr.number).state == "open"
    assert merge_owner.admissions(case.store) == queued
    assert hint_path(case.store.root, N).read_text() == hint
    assert not retry_marker(case.store.root, N).exists()
    assert "merge-queued" in message
    assert pr.head_sha[:7] in message
    assert "ci" in message.casefold() and "green" in message.casefold()
    assert "--discard-queued" in message


def test_retry_with_the_discard_flag_closes_as_today(green_queued_case):
    case, main, pr = green_queued_case
    hint = "Repair the implementation after explicitly discarding the queued work."

    assert main(["orch", "retry", str(N), "--discard-queued", "--hint", hint]) == 0

    assert case.tracker.get_pr(pr.number).state == "closed"
    assert pr.head not in case.tracker.deleted_branches
    assert "ready" in case.tracker.get_issue(N).labels
    assert retry_marker(case.store.root, N).exists()
    assert hint_path(case.store.root, N).read_text().strip() == hint
