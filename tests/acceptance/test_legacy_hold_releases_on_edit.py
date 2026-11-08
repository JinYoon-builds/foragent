"""Acceptance test for legacy-hold-releases-on-edit.

#2458 refuses `fa orch retry N` on a same-class review hold until an approved order edit
changes the issue's contract, comparing against the contract recorded when the hold was
set (hold-contracts/<n>.json). Holds set before #2458 have no such record. Such a hold
must use the approval in force when it was set as its contract: an approved edit that
changes the Goal releases it. The refusal of an unchanged legacy hold, which already
holds on main, is guarded separately in test_legacy_hold_refusal_guard.py.

The hold is reached through the real runner with fake agents (the same path as
test_retry_same_class_refuse); the legacy shape is the same hold without its recorded
contract.
"""

from datetime import timedelta
import re
import shutil

from foragent.issue import body_hash, parse_body
from foragent.reasons import Reason
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_orch_review_handoff import bind_cli
from test_review_decision_hold_reader import pending_review_decision
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


NARROWED = "Only persist the saved result before the reply is sent; nothing else changes."


def legacy_held_case(git_history, tmp_path, monkeypatch):
    """A two-round same-class hold as it looked before #2458: no recorded hold contract."""
    case, _script, _question = pending_review_decision(git_history, tmp_path, monkeypatch)
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS
    shutil.rmtree(case.store.root / "hold-contracts", ignore_errors=True)
    assert not (case.store.root / "hold-contracts").exists()
    if case.tracker.find_pr("foragent/issue-1") is None:
        case.tracker.create_pr(head="foragent/issue-1", base="main", title="Attempt", body="Attempt")
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    case.clock[0] += timedelta(minutes=1)
    return case, main


def with_goal(body, goal):
    """The issue body with its Goal section replaced; every other section kept."""
    current = parse_body(body).goal.strip()
    assert current and current in body, "The fixture issue must have a Goal section"
    edited = body.replace(current, goal, 1)
    assert parse_body(edited).goal.strip() == goal
    return edited


def approve_edit(case, body):
    """An approved order edit recorded during the hold, as the gate saves a re-approval."""
    held = case.store.load_approval(1)
    flagged = case.store.load_flag(1).flagged_at
    case.clock[0] += timedelta(minutes=5)
    if body != case.tracker.get_issue(1).body:
        case.tracker.edit_body(1, body)
    case.clock[0] += timedelta(minutes=5)
    at = case.clock[0].isoformat()
    assert at > flagged
    case.store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs=dict(held.acceptance_blobs),
                             approved_by=held.approved_by, approved_at=at)
    case.clock[0] += timedelta(minutes=1)


def says_how_to_proceed(text):
    words = " ".join(text.casefold().split())
    return ("order edit" in words and "goal" in words
            and all(re.search(rf"\b{word}", words) for word in ("narrow", "split", "redesign")))


def test_approved_goal_edit_releases_a_legacy_hold(git_history, tmp_path, monkeypatch, capsys, kicks):
    case, main = legacy_held_case(git_history, tmp_path, monkeypatch)
    old = case.store.load_approval(1)
    approve_edit(case, with_goal(old.body, NARROWED))
    assert parse_body(case.store.load_approval(1).body).goal != parse_body(old.body).goal
    capsys.readouterr()
    kicked = list(kicks)

    code = main(["orch", "retry", "1"])
    out = capsys.readouterr()
    text = out.out + out.err

    assert code in (0, None), f"An approved Goal edit must release a hold with no recorded contract: {out!r}"
    assert not says_how_to_proceed(text), f"The retry must not be refused: {out!r}"
    assert "queued implementation retry" in text, f"The retry must be queued: {out!r}"
    assert "ready" in case.tracker.get_issue(1).labels, "The released retry must queue the issue"
    assert len(kicks) == len(kicked) + 1, "The queued retry kicks one gate tick"
