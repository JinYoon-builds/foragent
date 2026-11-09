"""Acceptance: an approved issue starts only after the existing issue it waits on is delivered.

Order blocked-by-existing (Jin r1301, PM r1334), issue gate-waits-for-existing-blocker. WAITING
(#2772) is approved and ready ahead of time with blocked_by an already published issue BLOCKER
(#2552). A blocker is finished when its Done when passes on main, whatever its closure says;
in this world its Done when passes once a main commit adds its proof file. The gate runs
through `scheduler.tick` with free capacity; the orchestrator inbox is read through
`fa orch inbox --json`.

Statement 1 (an open blocker whose Done when fails holds the issue) already holds on main for a
lone blocker; here it is checked as the first phase of every scenario below, each of which fails
on main through a later phase. Pure regression controls live in
test_gate_existing_blocker_guard.py.
"""

from pathlib import Path
import re

import pytest

from foragent import scheduler
from gate_existing_blocker_world import (  # noqa: F401
    BLOCKER, OTHER_BLOCKER, OTHER_WAITING, WAITING, advance, close_blocker, deliver, inbox_rows,
    make_world, mentions_issue, mentions_looks_delivered, mentions_undelivered_close,
    offline_gate_world, started,
)
from test_scheduler import BODY, ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401

pytestmark = pytest.mark.usefixtures("offline_gate_world")

# GitHub's close reasons other than "completed": not planned, duplicate, and a close that
# carries no reason at all. None of them is a merge.
UNMERGED = ["not_planned", "duplicate", None]
UNMERGED_IDS = ["not-planned", "duplicate", "no-reason"]


def _hold_while_open(ctx, capsys, passes=2):
    """Statement 1: open blocker, Done when failing on main, free capacity: no start, no request."""
    for _ in range(passes):
        scheduler.tick(ctx)
        assert not started(ctx), (
            f"#{BLOCKER} is open and its Done when fails on main: #{WAITING} must not start")
        assert ctx.launched == [] and ctx.store.active_runs() == []
        advance(ctx, 6)
    rows = inbox_rows(capsys, ctx)
    assert not any(mentions_looks_delivered(row) or mentions_undelivered_close(row) for row in rows), (
        f"No request may be sent while #{BLOCKER} is open and undelivered: {rows}")


def test_delivered_blocker_closed_not_planned_starts_its_waiter_on_the_first_pass(
        ctx, monkeypatch, capsys, git_history):
    """Statement 2: the Done when on main decides, not the close reason (#2506 sat behind a
    not-planned close). Duplicate and reason-less closes already start on main and are guarded
    in test_gate_existing_blocker_guard.py. Rejects holding on an unmerged close, a start one pass late, a doubled
    start, and an undelivered-close request about a delivered blocker."""
    make_world(ctx, monkeypatch, git_history, BODY)
    _hold_while_open(ctx, capsys)

    reason = "not_planned"
    deliver(ctx)
    close_blocker(ctx, reason)
    scheduler.tick(ctx)
    assert ctx.launched == [WAITING], (
        f"#{BLOCKER} closed (reason={reason!r}) and its Done when passes on main: the first gate "
        f"pass must start #{WAITING}; launched {ctx.launched}")
    for _ in range(2):
        advance(ctx, 6)
        scheduler.tick(ctx)
        assert ctx.launched == [WAITING], f"#{WAITING} starts once; launched {ctx.launched}"
    rows = inbox_rows(capsys, ctx)
    assert not any(mentions_undelivered_close(row) for row in rows), (
        f"#{BLOCKER} was delivered; it did not close without delivering: {rows}")


@pytest.mark.parametrize("reason", UNMERGED, ids=UNMERGED_IDS)
def test_undelivered_blocker_closed_never_starts_and_tells_orchestrator_once(
        ctx, monkeypatch, capsys, git_history, reason):
    """Statement 3. Rejects starting on any close, a silent hold, and a request repeated by later
    passes."""
    make_world(ctx, monkeypatch, git_history, BODY)
    _hold_while_open(ctx, capsys)

    close_blocker(ctx, reason)
    for _ in range(3):  # the first pass after the close, then two more
        scheduler.tick(ctx)
        assert not started(ctx), (
            f"#{BLOCKER} closed (reason={reason!r}) and its Done when still fails on main: "
            f"#{WAITING} must not start")
        assert ctx.store.active_runs() == []
        advance(ctx, 6)

    rows = inbox_rows(capsys, ctx)
    matches = [row for row in rows if mentions_undelivered_close(row)]
    assert len(matches) == 1, (
        f"The orchestrator inbox must get exactly one request naming #{WAITING}, #{BLOCKER} and "
        f"that #{BLOCKER} closed without delivering; got {len(matches)}: {rows}")
    assert not any(mentions_looks_delivered(row) for row in rows), (
        f"#{BLOCKER}'s Done when fails on main; nothing may call it delivered: {rows}")


@pytest.mark.parametrize("reason", UNMERGED, ids=UNMERGED_IDS)
def test_merged_blocker_starts_its_waiter_while_an_undelivered_close_keeps_holding(
        ctx, monkeypatch, capsys, git_history, reason):
    """Statements 1-3 in one gate world, two waiting issues in separate lanes.

    #2772 waits on #2552, #2773 waits on #2553 (both existing issues). While both are open and
    undelivered, neither starts. Then #2553 merges (its proof lands on main, closed as
    completed) and #2552 closes without delivering. The first gate pass starts #2773 (once) and
    not #2772, and the orchestrator gets one request about #2772/#2552 only. Rejects: starting
    on any close, holding every waiter on any close, a start one pass late, a doubled start, and
    a request about the merged pair.
    """
    make_world(ctx, monkeypatch, git_history, BODY, pairs=(
        (WAITING, BLOCKER, "orch"), (OTHER_WAITING, OTHER_BLOCKER, "view")))
    for _ in range(2):
        scheduler.tick(ctx)
        assert not started(ctx, WAITING) and not started(ctx, OTHER_WAITING), (
            "Open, undelivered existing blockers hold both waiting issues, even with free capacity")
        assert ctx.launched == [] and ctx.store.active_runs() == []
        advance(ctx, 6)

    deliver(ctx, OTHER_BLOCKER)
    close_blocker(ctx, "completed", OTHER_BLOCKER)
    close_blocker(ctx, reason, BLOCKER)
    scheduler.tick(ctx)
    assert ctx.launched == [OTHER_WAITING], (
        f"The first gate pass after #{OTHER_BLOCKER} merged must start #{OTHER_WAITING}, and "
        f"#{WAITING} must not start because #{BLOCKER} closed without delivering "
        f"(reason={reason!r}); launched {ctx.launched}")
    for _ in range(2):
        advance(ctx, 6)
        scheduler.tick(ctx)
        assert ctx.launched == [OTHER_WAITING], (
            f"#{OTHER_WAITING} starts once and #{WAITING} never starts; launched {ctx.launched}")

    rows = inbox_rows(capsys, ctx)
    about_close = [row for row in rows if mentions_undelivered_close(row)]
    assert len(about_close) == 1, (
        f"Exactly one orchestrator request must name #{WAITING}, #{BLOCKER} and that #{BLOCKER} "
        f"closed without delivering; got {len(about_close)}: {rows}")
    assert not any(mentions_issue(row, OTHER_BLOCKER) for row in rows), (
        f"A merged blocker (#{OTHER_BLOCKER}) is not a reason to tell the orchestrator anything")


def test_open_blocker_whose_done_when_passes_is_reported_once_as_looking_delivered(
        ctx, monkeypatch, capsys, git_history):
    """Statement 4 (#2673 stopped as 'already passes on main'). Rejects no request, a request
    repeated by later passes, a request before the Done when passes, and an undelivered-close
    request for an issue that is still open."""
    make_world(ctx, monkeypatch, git_history, BODY)
    _hold_while_open(ctx, capsys)

    deliver(ctx)
    for _ in range(3):
        scheduler.tick(ctx)
        advance(ctx, 6)

    rows = inbox_rows(capsys, ctx)
    delivered = [row for row in rows if mentions_looks_delivered(row)]
    assert len(delivered) == 1, (
        f"#{BLOCKER} is open but its Done when passes on main: the orchestrator inbox must get "
        f"exactly one request saying #{BLOCKER} looks delivered; got {len(delivered)}: {rows}")
    assert not any(mentions_undelivered_close(row) for row in rows), (
        f"#{BLOCKER} is still open; it did not close without delivering: {rows}")


def test_closing_a_blocker_reported_as_delivered_starts_its_waiter(
        ctx, monkeypatch, capsys, git_history):
    """Statements 4 then 2: the orchestrator closes the delivered blocker as not planned (it was
    delivered by other work); the next gate pass starts the waiting issue, once."""
    make_world(ctx, monkeypatch, git_history, BODY)
    _hold_while_open(ctx, capsys)
    deliver(ctx)
    scheduler.tick(ctx)
    advance(ctx, 6)
    assert sum(mentions_looks_delivered(row) for row in inbox_rows(capsys, ctx)) == 1, (
        f"The first pass after #{BLOCKER}'s Done when passes on main must report it as delivered")

    close_blocker(ctx, "not_planned")
    scheduler.tick(ctx)
    assert started(ctx) and ctx.launched.count(WAITING) == 1, (
        f"#{BLOCKER} is closed and its Done when passes on main: #{WAITING} must start once on "
        f"the first pass; launched {ctx.launched}")
    advance(ctx, 6)
    scheduler.tick(ctx)
    assert ctx.launched.count(WAITING) == 1, f"#{WAITING} starts once; launched {ctx.launched}"


def _skill_paragraphs():
    root = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator"
    for path in sorted(root.rglob("*.md")):
        for paragraph in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            yield path, " ".join(paragraph.split())


APPROVE_AHEAD = re.compile(
    r"approv\w*[^.]{0,160}\b(ahead|now|right away|immediately|before\b[^.]{0,40}merg)", re.I)
CLOSE_DELIVERED = re.compile(r"\bclos\w*[^.]{0,160}\bdelivered\b|\bdelivered\b[^.]{0,160}\bclos\w*",
                             re.I)
PASSES_ON_MAIN = re.compile(r"done when[^.]{0,80}(already\s+)?pass\w*[^.]{0,40}\bmain\b", re.I)
NO_PERSON = re.compile(
    r"(without|no need to|do not|don'?t|never)\s+(ask\w*|wait\w*\s+for|check\w*\s+with)[^.]{0,40}"
    r"\b(a\s+|the\s+)?(person|human|jin|user|owner)", re.I)


def test_orchestrator_skill_approves_waiting_work_ahead_with_blocked_by():
    """skills/fa-orchestrator: approve now with the existing issue in blocked_by."""
    found = [
        (path, text) for path, text in _skill_paragraphs()
        if "blocked_by" in text and re.search(r"existing issue", text, re.I)
        and APPROVE_AHEAD.search(text)
    ]
    assert found, (
        "skills/fa-orchestrator must say: approve work that waits on an existing issue ahead, "
        "with that issue in blocked_by")


def test_orchestrator_skill_closes_an_issue_whose_done_when_passes_as_delivered():
    """skills/fa-orchestrator: an issue whose Done when already passes on main is closed as
    delivered without asking a person."""
    found = [
        (path, text) for path, text in _skill_paragraphs()
        if PASSES_ON_MAIN.search(text) and CLOSE_DELIVERED.search(text) and NO_PERSON.search(text)
    ]
    assert found, (
        "skills/fa-orchestrator must say: close an issue whose Done when already passes on main "
        "as delivered, without asking a person")
