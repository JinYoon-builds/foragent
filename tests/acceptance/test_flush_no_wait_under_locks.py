"""With merge_owner on, `fa order flush` never waits for a merge train while holding its locks.

Public entry points: ``order.publish(defer=...)`` retains an order for a boundary,
``order_notes.append`` retains a note, ``order_flush.flush`` is what ``fa order flush`` runs,
``order_prepare_owner.claim`` is how ``fa order prepare`` claims its keys, and the merge owner's
own pass (``order_publications.check_pending`` then ``merge_owner.tick``) lands queued work.

Git is real (an offline origin, pushes emulated by fetch); GitHub is the fake tracker; red
measurement is the fake used by the owner publication tests. A merge train is a live
``refs/heads/foragent/batch-merge-*`` ref on the origin, exactly what the owner creates.
Waiting is observed at ``time.sleep`` in the flushing thread only; no lock name or production
helper is replaced. No signal is sent and no process is started besides git.
"""

import threading
import time

from conftest import git_command
from test_order import offline_publication  # noqa: F401
from test_order_publications import NAME, case, submit  # noqa: F401
from foragent import main_write_requests, merge_owner, order_flush, order_notes, order_prepare, order_prepare_owner, order_publications
from foragent.scheduler import TickReport

REAL_SLEEP = time.sleep
JOIN_S = 60


class Waited(Exception):
    """Raised in the flushing thread when it starts waiting for the merge train."""


def world(case, offline_publication):
    """An order and a note retained for one boundary, and a real Git checkout for the flush."""
    root, _config, _tracker, git, calls, origin = offline_publication
    report = submit(case, defer="boundary")
    assert report.waiting_for == "boundary"
    order_notes.append(case.store, kind="decision", boundary="boundary", content="Retained decision")
    case.ctx.git, case.ctx.repo_root = git, root
    base = git_command(origin, "rev-parse", "main")
    # Preparation reviewed the tests on the real base this checkout publishes onto.
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"]["base_revision"] = base
    order_prepare._write(path, record)
    train = f"refs/heads/foragent/batch-merge-{base[:8]}-1"
    git_command(origin, "update-ref", train, base)
    return root, git, calls, origin, base, train


def flush(case, root, git, boundary="boundary"):
    return order_flush.flush(root, case.config, case.tracker, git, case.store,
                             repo=case.config.repo, boundary=boundary)


def in_thread(target):
    """Run ``target`` in a daemon thread; returns (thread, outcome dict)."""
    outcome = {}

    def body():
        try:
            outcome["result"] = target()
        except BaseException as exc:  # Every worker failure is returned to the test.
            outcome["error"] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, outcome


def owner_passes(case, count=3):
    actions = []
    for _ in range(count):
        report = TickReport()
        order_publications.check_pending(case.ctx, report)
        merge_owner.tick(case.ctx, report)
        actions += report.actions
    return actions


def assert_landed_once(case, origin):
    titles = [issue.title for issue in case.tracker.list_issues()]
    assert titles.count("New behaviour") == 1, titles
    assert titles.count("Publication") == 1, titles
    assert git_command(origin, "show", f"main:{NAME}") == "def test_new(): pass"
    # #2161: orders no longer write docs/specs files.
    assert git_command(origin, "ls-tree", "--name-only", "main", "docs/specs/") == ""
    decisions = git_command(origin, "show", "main:docs/decisions.md")
    assert decisions.count("Retained decision") == 1, decisions
    assert decisions == "- Retained decision"


def test_flush_returns_during_a_merge_train_and_the_owner_lands_its_orders_and_notes(
        case, offline_publication, monkeypatch):
    root, git, calls, origin, base, train = world(case, offline_publication)
    waits, flusher = [], {}

    def sleep(seconds):
        if threading.current_thread() is flusher.get("thread"):
            waits.append(seconds)
            raise Waited(f"flush slept {seconds}s while a merge train was in flight")
        REAL_SLEEP(seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    thread, outcome = in_thread(lambda: flush(case, root, git))
    flusher["thread"] = thread
    thread.join(JOIN_S)
    assert not thread.is_alive(), "fa order flush did not return while a merge train was in flight"
    assert not waits, f"fa order flush waited for the merge train: {outcome.get('error')}"
    assert "error" not in outcome, outcome
    assert isinstance(outcome["result"], dict) and outcome["result"]["boundary"] == "boundary"
    # The train still owns main: the flush returned without changing it.
    assert git_command(origin, "rev-parse", "main") == base
    assert not any(args[0] == "push" for args in calls)

    # The train ends; the merge owner lands what the flush handed over, once.
    git_command(origin, "update-ref", "-d", train)
    monkeypatch.setattr(time, "sleep", REAL_SLEEP)
    owner_passes(case)
    assert_landed_once(case, origin)
    assert [row["status"] for row in order_publications.requests(case.store)
            if row["plan"]["id"] == "publication"] == ["published"]
    assert not main_write_requests.pending(case.store)
    landed = git_command(origin, "rev-parse", "main")
    owner_passes(case)
    assert git_command(origin, "rev-parse", "main") == landed
    assert_landed_once(case, origin)
    # A second flush of the same boundary hands nothing over again.
    again = flush(case, root, git)
    owner_passes(case, 1)
    assert again["boundary"] == "boundary"
    assert git_command(origin, "rev-parse", "main") == landed
    assert_landed_once(case, origin)


def test_a_waiting_flush_blocks_neither_another_flush_nor_a_preparation_claim(
        case, offline_publication, monkeypatch):
    root, git, _calls, origin, _base, train = world(case, offline_publication)
    order_notes.append(case.store, kind="idea", boundary="other", content="Another session's idea")
    probes, flusher = {}, {}
    folder = case.store.root / "prepare"

    def claim():
        record = {}
        taken = order_prepare_owner.claim(folder, record, ["probe"], lambda: None, wait_s=3,
                                          sleep=lambda seconds: REAL_SLEEP(min(seconds, 0.05)))
        return taken, record

    def probe():
        """Runs while the first flush is waiting on the train, as two other sessions would.

        The first flush stays frozen inside its wait. The train ref is removed first, so the other
        flush meets no train of its own: it can only be held up by what the waiting flush holds.
        """
        git_command(origin, "update-ref", "-d", train)
        claimer, claimed = in_thread(claim)
        claimer.join(10)
        other, flushed = in_thread(lambda: flush(case, root, git, "other"))
        other.join(15)
        probes.update(claim=(claimer.is_alive(), claimed), other=(other, flushed, other.is_alive()))

    def sleep(seconds):
        if threading.current_thread() is flusher.get("thread") and "claim" not in probes:
            probe()  # The train has ended; the wait may finish.
            return
        REAL_SLEEP(min(seconds, 0.05) if threading.current_thread() is flusher.get("thread") else seconds)

    monkeypatch.setattr(time, "sleep", sleep)
    thread, outcome = in_thread(lambda: flush(case, root, git))
    flusher["thread"] = thread
    thread.join(JOIN_S)
    assert not thread.is_alive(), "The first flush never finished"
    if "claim" in probes:
        claim_alive, claimed = probes["claim"]
        other, flushed, other_alive = probes["other"]
        assert not claim_alive and claimed.get("result", (False,))[0] is True, (
            f"A preparation claim was blocked by a flush waiting on the merge train: {claimed}")
        assert claimed["result"][1]["claimed"] == ["probe"]
        assert not other_alive, "Another session's flush was blocked by a flush waiting on the merge train"
        assert "error" not in flushed, flushed
    else:
        # The flush handed its work over without waiting; nothing could be blocked by a wait.
        claimer, claimed = in_thread(claim)
        claimer.join(10)
        assert claimed.get("result", (False,))[0] is True, claimed
        other, flushed = in_thread(lambda: flush(case, root, git, "other"))
        other.join(15)
        assert not other.is_alive() and "error" not in flushed, flushed
    assert "error" not in outcome, outcome
    other.join(JOIN_S)
    assert flushed["result"]["boundary"] == "other"
    if git_command(origin, "for-each-ref", train):
        git_command(origin, "update-ref", "-d", train)  # The train ends before the owner's pass.
    monkeypatch.setattr(time, "sleep", REAL_SLEEP)
    owner_passes(case)
    # Both sessions' work lands exactly once.
    assert_landed_once(case, origin)
    ideas = git_command(origin, "show", "main:docs/ideas.md")
    assert ideas.count("Another session's idea") == 1, ideas
