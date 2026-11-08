"""Deterministic interleavings at the queue's unlocked handoffs."""

from copy import deepcopy
import os
from types import SimpleNamespace

import pytest

from test_order import offline_publication  # noqa: F401
from test_order_publications import case, submit, review_plan  # noqa: F401
from foragent import file_locks, merge_owner, order, order_flush, order_notes, order_prepare
from foragent import order_prepare_owner, order_publications, order_publications_process, order_submissions
from foragent import state_claims
from foragent.scheduler import TickReport


def legacy(case):
    case.plan["id"] = "legacy"
    case.plan["issues"][0].pop("statements")


@pytest.mark.parametrize("migrated", [False, True])
def test_overlapping_flushes_cannot_replay_an_older_revision(case, monkeypatch, migrated):
    legacy(case)

    def retain():
        if migrated and not order_submissions.entries(case.store):
            order_notes.append_entry(case.store, {
                "id": case.plan["id"], "plan": deepcopy(case.plan), "approve": True,
                "approve_keys": [], "approve_numbers": [], "boundary": "turn", "status": "waiting",
            }, journal="orders.jsonl")
        else:
            order_submissions.retain(case.plan, case.config.repo, "turn", approve=True,
                                     approve_keys=[], approve_numbers=[], language="en")

    def flush():
        return order_flush.flush(case.root, case.config, case.tracker, case.git, case.store,
                                 repo=case.config.repo, boundary="turn")

    retain()
    enqueue = order_publications.enqueue
    paused = False

    def interleave(*args, **kwargs):
        nonlocal paused
        if not paused:
            paused = True
            case.plan["summary"] = "v2"
            retain()
            flush()  # B hands off v1 then v2 before A hands off its selected v1.
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(order_publications, "enqueue", interleave)
    flush()
    rows = order_publications.requests(case.store)
    assert [(row["plan"]["summary"], row["status"]) for row in rows] == [
        ("Goal", "superseded"), ("v2", "pending")]
    flush()
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    assert order_publications.requests(case.store)[-1]["status"] == "published"
    assert not order_submissions.entries(case.store)


@pytest.mark.parametrize("launcher,name", [(merge_owner, "merge-owner"),
                                          (order_publications_process, "order-publication-checks")])
def test_parent_handoff_does_not_wait_for_child_adoption_under_tick(case, monkeypatch, launcher, name):
    submit(case)
    child_lock = []
    tokens = []

    def spawn(argv, **kwargs):
        tokens.append(argv[4])
        hold = file_locks.file_lock(case.store.root / f"{name}.lock")
        hold.__enter__()  # The descheduled child is inside its local adoption transition.
        child_lock.append(hold)
        return SimpleNamespace(pid=os.getpid())

    def sleep(seconds):
        pytest.fail(f"sleep({seconds}) during a nested ownership transition")

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(file_locks.time, "sleep", sleep)
    try:
        with case.store.tick_lock() as owned:
            assert owned
            report = TickReport()
            launcher.start(case.ctx, report)
            assert len(report.actions) == 1 and report.actions[0].endswith("started")
    finally:
        for hold in child_lock:
            hold.__exit__(None, None, None)
        for token in tokens:
            state_claims.release(case.store.root / f"{name}.intent", token)


def test_owner_failure_does_not_restore_a_superseded_snapshot(case, monkeypatch):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    apply = order_publications._apply

    def replaced(ctx, report, row):
        apply(ctx, report, row)
        case.plan["summary"] = "Replacement during preflight"
        review_plan(case)
        submit(case)
        raise order.PlanError("Old preflight failed")

    monkeypatch.setattr(order_publications, "_apply", replaced)
    order_publications.apply_next(case.ctx, TickReport())
    old, new = order_publications.requests(case.store)
    assert old["status"] == "superseded" and new["status"] == "pending"


def test_revision_replay_finishes_retirement_interrupted_after_replacement_was_saved(case, monkeypatch):
    original = deepcopy(case.plan)

    def enqueue(revision):
        publication = order._publication_checks(case.plan, case.root, case.config, case.tracker,
                                               repo=case.config.repo, git=case.git, queued=True)
        return order_publications.enqueue(case.plan, case.root, case.config, case.config.repo, publication,
                                          approve=True, approve_keys=[], approve_numbers=[],
                                          submission_revision=[1, revision])

    enqueue("v1")
    case.plan["summary"] = "v2"
    review_plan(case)
    save = order_publications._save

    def stop_after_save(store, row):
        save(store, row)
        if row["plan"]["summary"] == "v2":
            raise OSError("stopped after retaining replacement")

    with monkeypatch.context() as patch:
        patch.setattr(order_publications, "_save", stop_after_save)
        with pytest.raises(OSError, match="stopped after"):
            enqueue("v2")
    case.plan = original
    review_plan(case)
    request = enqueue("v1")
    old, new = order_publications.requests(case.store)
    assert old["status"] == "superseded" and new["status"] == "pending"
    assert request == new and new["submission_revision"] == [1, "v2"]


def test_late_legacy_handoff_preserves_the_owners_newer_commit_intent(case, monkeypatch):
    legacy(case)
    order_submissions.retain(case.plan, case.config.repo, "turn", approve=True,
                             approve_keys=[], approve_numbers=[], language="en")
    monkeypatch.setattr(order_flush.recovery, "_find", lambda *args: "legacy-commit")
    enqueue = order_publications.enqueue

    def owner_advanced(*args, **kwargs):
        row = enqueue(*args, **kwargs)
        current = order_publications._load(case.store, row["id"])
        current.update(status="applying", commit="owner-commit")
        order_publications._save(case.store, current)
        return row

    monkeypatch.setattr(order_publications, "enqueue", owner_advanced)
    order_flush.flush(case.root, case.config, case.tracker, case.git, case.store,
                      repo=case.config.repo, boundary="turn")
    assert order_publications.requests(case.store)[0]["commit"] == "owner-commit"


def test_legacy_landed_request_is_reserved_before_recovering_issue_effects(case, monkeypatch):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.git.remote.lost = True
    order_publications.apply_next(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    row["status"] = "pending"  # The earlier writer retained a commit without an applying transition.
    order_publications._save(case.store, row)
    check = order_publications._resume_check

    def replaced(*args, **kwargs):
        publication = check(*args, **kwargs)
        submit(case, approve=False)
        return publication

    monkeypatch.setattr(order_publications, "_resume_check", replaced)
    order_publications.apply_next(case.ctx, TickReport())
    old, new = order_publications.requests(case.store)
    assert old["status"] == "published" and "superseded_by" not in old
    assert new["status"] == "pending"


@pytest.mark.parametrize("recovering", [False, True])
def test_successful_preparation_claim_between_preflight_and_writes_is_preserved(case, monkeypatch, recovering):
    legacy(case)
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    if recovering:
        case.git.remote.lost = True
        order_publications.apply_next(case.ctx, TickReport())
    check = order._publication_checks
    claims = []

    def claim_after_check(*args, **kwargs):
        publication = check(*args, **kwargs)
        path = order_prepare._record_path(case.store, case.plan)
        record = order_prepare._read(path, case.plan)
        claims.append(order_prepare_owner.claim(path.parent, record, ["new"],
                                                lambda: order_prepare._write(path, record)))
        return publication

    monkeypatch.setattr(order, "_publication_checks", claim_after_check)
    order_publications.apply_next(case.ctx, TickReport())
    assert claims == [True]
    assert not case.tracker.list_issues(), "A successful claim must prevent statementless issue writes"
    assert order_publications.requests(case.store)[0]["status"] != "published"


def test_publication_reserves_only_its_routes_until_issue_effects_finish(case, monkeypatch):
    legacy(case)
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    publish = order._publish_issues
    claims = []

    def while_publishing(*args, **kwargs):
        for identity, key in ((case.plan["id"], "new"), (case.plan["id"], "other"), ("other", "new")):
            path = order_prepare._record_path(case.store, {"id": identity})
            record = order_prepare._read(path, {"id": identity})
            claims.append(order_prepare_owner.claim(path.parent, record, [key],
                                                    lambda: order_prepare._write(path, record)))
        return publish(*args, **kwargs)

    monkeypatch.setattr(order, "_publish_issues", while_publishing)
    order_publications.apply_next(case.ctx, TickReport())
    assert claims == [False, True, True]
    assert order_publications.requests(case.store)[0]["status"] == "published"
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    assert order_prepare_owner.claim(path.parent, record, ["new"], lambda: order_prepare._write(path, record))


def test_preparing_a_newly_created_child_as_an_edit_respects_its_origin_reservation(case, monkeypatch):
    legacy(case)
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    create = case.tracker.create_issue
    observed = []

    def during_creation(title, body, labels):
        number = create(title, body, labels)
        if order._child_marker(case.plan["id"], "new") in body:
            edit = {"id": "another-order", "title": "Prepare existing child", "summary": "Goal",
                    "spec_markdown": "# Spec", "issues": [], "edits": [
                        {"number": number, "goal": "Observable behaviour", "out_of_scope": "Other behaviour",
                         "statements": ["The behaviour works"], "done_when": ["true"]}]}
            observed.extend(order_prepare.prepare(edit, case.root, case.config, case.git, case.store,
                                                  lambda: case.tracker))
        return number

    monkeypatch.setattr(case.tracker, "create_issue", during_creation)
    monkeypatch.setattr(order_prepare, "_write_tests", lambda *a, **kw: pytest.fail("Preparation bypassed publication"))
    order_publications.apply_next(case.ctx, TickReport())
    assert len(observed) == 1 and observed[0]["status"] == "waiting"
    assert order_publications.requests(case.store)[0]["status"] == "published"
