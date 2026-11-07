"""Boundaries of the retry guard for green merge-queued work (#2420)."""

import pytest

from foragent import cli, merge_owner
from foragent.run_previous import hint_path, retry_marker
from helpers.merge_world import world  # noqa: F401
from helpers.review_case import bind_cli
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support


N = 5


@pytest.fixture
def queued(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [N])
    pr = case.tracker.get_pr(support.pr_for(case, N).number)
    assert merge_owner.admissions(case.store)
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "RunContext", lambda **kwargs: case.ctx)
    return case, main, pr


@pytest.mark.parametrize("checks", ["fail", "pending", "none"])
def test_retry_without_green_ci_closes_as_today(queued, checks):
    case, main, pr = queued
    case.tracker.set_pr_checks(pr.number, checks)

    assert main(["orch", "retry", str(N)]) == 0

    assert case.tracker.get_pr(pr.number).state == "closed"
    assert retry_marker(case.store.root, N).exists()


def test_refused_retry_keeps_no_hint_comment_or_labels(queued, capsys):
    case, main, pr = queued
    labels = list(case.tracker.get_issue(N).labels)
    comments = len(case.tracker.list_comments(N))
    outbox = case.store.github_outbox()
    capsys.readouterr()

    assert main(["orch", "retry", str(N), "--hint", "redo it"]) != 0

    err = capsys.readouterr().err
    assert "--discard-queued" in err and pr.head_sha[:7] in err and f"PR #{pr.number}" in err
    assert not hint_path(case.store.root, N).exists()
    assert len(case.tracker.list_comments(N)) == comments
    assert list(case.tracker.get_issue(N).labels) == labels
    assert case.store.github_outbox() == outbox
    assert not retry_marker(case.store.root, N).exists()


def test_retry_with_owner_off_ignores_the_queue(queued):
    case, main, pr = queued
    from dataclasses import replace

    monkeypatch_config = replace(case.ctx.config, merge_owner=False)
    object.__setattr__(case.ctx, "config", monkeypatch_config)

    assert main(["orch", "retry", str(N)]) == 0

    assert case.tracker.get_pr(pr.number).state == "closed"


def test_guard_matches_only_the_queued_issue(queued):
    from foragent import orch

    case, main, pr = queued
    # The guard matches only the queued issue's own entry.
    assert orch._green_queued(case.ctx, N + 100) is None
    assert orch._green_queued(case.ctx, N) is not None
