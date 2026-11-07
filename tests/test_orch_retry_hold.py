"""Boundaries of the same-class retry refusal: the contract's content decides, never approval time."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import actions, board, orch_retry_hold
from foragent.reasons import Reason
from foragent.state import StateStore

LOCK = "tests/acceptance/test_feature.py"
T0 = datetime(2026, 11, 7, 10, tzinfo=UTC)


def body(goal="Persist the result.", command="uv run pytest tests/acceptance/test_feature.py", notes=""):
    return (f"## Goal\n{goal}\n\n## Done when\n```sh\n{command}\n```\n\n"
            f"## Preparation notes\n- checked on main{notes}\n")


@pytest.fixture
def held(tmp_path, monkeypatch):
    """An issue held for two same-class rounds through the real flag(), on its first approval."""
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.save_approval(1, body=body(), body_sha="sha-1", acceptance_blobs={LOCK: "blob-1"},
                        approved_by="jin", approved_at=T0.isoformat())
    monkeypatch.setattr(board, "sync_status", lambda *args, **kwargs: None)
    tracker = SimpleNamespace(add_labels=lambda *a: None, remove_labels=lambda *a: None, comment=lambda *a: None)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=lambda: clock[0])
    clock[0] += timedelta(minutes=5)
    actions.flag(ctx, 1, Reason.REVIEW_DEFECTS, "Two failed rounds of the same class; awaiting PM decision.")
    return ctx


def reapprove(ctx, **edit):
    """A later approval of the issue, as the gate records it during a hold."""
    blob = edit.pop("blob", "blob-1")
    later = (T0 + timedelta(hours=1)).isoformat()
    ctx.store.save_approval(1, body=body(**edit), body_sha="sha-new", acceptance_blobs={LOCK: blob},
                            approved_by="jin", approved_at=later)


def test_the_hold_refuses_a_retry_on_its_own_approval(held):
    text = orch_retry_hold.refusal(held, 1)
    assert text.startswith("#1 ") and "order edit" in text


@pytest.mark.parametrize("edit", [{}, {"notes": "\n- a preparation note added"}],
                         ids=["unchanged", "preparation-only"])
def test_reapproval_without_a_contract_change_stays_refused(held, edit):
    reapprove(held, **edit)
    assert orch_retry_hold.refusal(held, 1) is not None


@pytest.mark.parametrize("edit", [{"goal": "Persist the result before replying."},
                                  {"command": "uv run pytest tests/acceptance/test_other.py"},
                                  {"blob": "blob-2"}],
                         ids=["goal", "done-when", "locked-test"])
def test_an_approved_contract_edit_allows_retry(held, edit):
    reapprove(held, **edit)
    assert orch_retry_hold.refusal(held, 1) is None


def test_a_hold_without_its_recorded_contract_stays_refused(held):
    (held.store.root / "hold-contracts" / "1.json").unlink()
    reapprove(held, goal="Persist the result before replying.")
    assert orch_retry_hold.refusal(held, 1) is not None


def test_other_holds_are_not_refused(held):
    held.store.save_flag(1, Reason.REVIEW_DISPUTE, flagged_at=(T0 + timedelta(hours=2)).isoformat())
    assert orch_retry_hold.refusal(held, 1) is None


def decide(ctx, answer, *, minutes=60, issue=1):
    """The orchestrator closes the hold's review-decision request, as `fa orch request-status` does."""
    from foragent import orch_inbox, review_scoring

    ctx.store._now = lambda: T0 + timedelta(minutes=minutes)
    request = orch_inbox.add_request(ctx.store, "gate", f"Review hold for #{issue}",
                                     {"kind": review_scoring.REQUEST_KIND, "issue": issue})
    orch_inbox.set_request_status(ctx.store, request["id"], "done", answer=answer, require_answer=True)


@pytest.mark.parametrize("answer", ["ship with follow-up", "narrow", "Narrow: drop the cache part"])
def test_a_recorded_orchestrator_decision_after_the_hold_allows_retry(held, answer):
    decide(held, answer)
    assert orch_retry_hold.refusal(held, 1) is None


@pytest.mark.parametrize("case", [dict(answer="escalated to the PM as q3"), dict(answer="narrow", issue=2),
                                  dict(answer="narrow", minutes=1)],
                         ids=["escalation", "other-issue", "before-the-hold"])
def test_other_decisions_keep_the_retry_refused(held, case):
    decide(held, **case)
    assert orch_retry_hold.refusal(held, 1) is not None
