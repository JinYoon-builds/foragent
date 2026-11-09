"""Boundaries of the same-class retry refusal: the contract's content decides, never approval time."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import actions, board, orch_retry_hold
from foragent.issue import body_hash
from foragent.reasons import Reason, format_approval_comment
from foragent.state import StateStore

LOCK = "tests/acceptance/test_feature.py"
T0 = datetime(2026, 11, 7, 10, tzinfo=UTC)


def body(goal="Persist the result.", command="uv run pytest tests/acceptance/test_feature.py", notes=""):
    return (f"## Goal\n{goal}\n\n## Done when\n```sh\n{command}\n```\n\n"
            f"## Preparation notes\n- checked on main{notes}\n")


class Tracker:
    """Keeps the comments posted on the issue, as GitHub does."""

    def __init__(self):
        self.comments = []

    def add_labels(self, *args):
        pass

    def remove_labels(self, *args):
        pass

    def comment(self, n, text):
        self.comments.append(SimpleNamespace(body=text))

    def list_comments(self, n):
        return list(self.comments)


def approve(ctx, at, *, post=True, blob="blob-1", **edit):
    """An approval snapshot saved as the gate saves it: the record plus its approval comment."""
    text = body(**edit)
    ctx.store.save_approval(1, body=text, body_sha=body_hash(text), acceptance_blobs={LOCK: blob},
                            approved_by="jin", approved_at=at.isoformat())
    if post:
        ctx.tracker.comment(1, format_approval_comment(body_hash(text), {LOCK: blob}, "jin", at.isoformat()))


@pytest.fixture
def held(tmp_path, monkeypatch):
    """An issue held for two same-class rounds through the real flag(), on its first approval."""
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    monkeypatch.setattr(board, "sync_status", lambda *args, **kwargs: None)
    ctx = SimpleNamespace(store=store, tracker=Tracker(), now=lambda: clock[0])
    approve(ctx, T0)
    clock[0] += timedelta(minutes=5)
    actions.flag(ctx, 1, Reason.REVIEW_DEFECTS, "Two failed rounds of the same class; awaiting PM decision.")
    return ctx


def reapprove(ctx, **edit):
    """A later approval of the issue, as the gate records it during a hold."""
    approve(ctx, T0 + timedelta(hours=1), **edit)


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
    """No recorded contract and no held run to read it back from: the hold is unknown, so it stands."""
    (held.store.root / "hold-contracts" / "1.json").unlink()
    reapprove(held, goal="Persist the result before replying.")
    assert orch_retry_hold.refusal(held, 1) is not None


def test_other_holds_are_not_refused(held):
    held.store.save_flag(1, Reason.REVIEW_DISPUTE, flagged_at=(T0 + timedelta(hours=2)).isoformat())
    assert orch_retry_hold.refusal(held, 1) is None


def legacy_case(tmp_path, monkeypatch, *, posted=True, ran=True):
    """A hold flagged before #2458: no recorded contract; what the gate kept from then decides."""
    clock = [T0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    monkeypatch.setattr(board, "sync_status", lambda *args, **kwargs: None)
    ctx = SimpleNamespace(store=store, tracker=Tracker(), now=lambda: clock[0], clock=clock)
    approve(ctx, T0, post=posted)
    clock[0] += timedelta(minutes=1)
    if ran:
        store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
        store.update_run(1, session_body=body())
        clock[0] += timedelta(minutes=1)
        store.finish_run(1, "blocked")
    clock[0] += timedelta(minutes=3)
    actions.flag(ctx, 1, Reason.REVIEW_DEFECTS, "Two failed rounds of the same class; awaiting PM decision.")
    (store.root / "hold-contracts" / "1.json").unlink()
    return ctx


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """The held run kept its approved body and the gate's approval comment kept the locked blobs."""
    return legacy_case(tmp_path, monkeypatch)


@pytest.mark.parametrize("edit", [{"goal": "Persist the result before replying."},
                                  {"command": "uv run pytest tests/acceptance/test_other.py"},
                                  {"blob": "blob-2"}],
                         ids=["goal", "done-when", "locked-test"])
def test_a_legacy_hold_releases_on_an_approved_contract_edit(legacy, edit):
    reapprove(legacy, **edit)
    assert orch_retry_hold.refusal(legacy, 1) is None


@pytest.mark.parametrize("edit", [{}, {"notes": "\n- a preparation note added"}],
                         ids=["unchanged", "preparation-only"])
def test_a_legacy_hold_stays_refused_without_a_contract_edit(legacy, edit):
    reapprove(legacy, **edit)
    assert orch_retry_hold.refusal(legacy, 1) is not None


def test_a_legacy_hold_reads_the_approval_in_force_when_flagged(tmp_path, monkeypatch):
    """An approval no later than the flag is the held one, whatever an earlier run's body said."""
    ctx = legacy_case(tmp_path, monkeypatch, posted=False)
    approve(ctx, T0 + timedelta(minutes=3), post=False, goal="Approved before the hold.", blob="blob-2")
    assert orch_retry_hold.refusal(ctx, 1) is not None


def test_a_legacy_hold_ignores_runs_started_after_the_flag(tmp_path, monkeypatch):
    """A run after the hold did not set it: its body is not the held contract."""
    legacy = legacy_case(tmp_path, monkeypatch, posted=False)
    legacy.clock[0] += timedelta(minutes=1)
    legacy.store.start_run(1, branch="foragent/issue-1", worktree="wt")
    legacy.store.update_run(1, session_body=body(goal="Persist the result before replying."))
    legacy.store.finish_run(1, "blocked")
    approve(legacy, T0 + timedelta(hours=1), post=False, goal="Persist the result before replying.")
    assert orch_retry_hold.refusal(legacy, 1) is None  # The held run's own body still decides.


@pytest.mark.parametrize(("edit", "released"), [({"blob": "blob-2"}, True), ({"goal": "Persist it first."}, False)],
                         ids=["locked-test", "goal"])
def test_a_legacy_hold_with_no_held_body_reads_the_blobs_in_force(tmp_path, monkeypatch, edit, released):
    """No run kept the held body: the approval comment still holds the locked blobs; the Goal is unknown."""
    ctx = legacy_case(tmp_path, monkeypatch, ran=False)
    reapprove(ctx, **edit)
    assert (orch_retry_hold.refusal(ctx, 1) is None) is released


@pytest.mark.parametrize(("edit", "released"), [({"goal": "Persist it first."}, True), ({"blob": "blob-2"}, False)],
                         ids=["goal", "locked-test"])
def test_a_legacy_hold_with_no_approval_comment_never_borrows_current_blobs(tmp_path, monkeypatch, edit, released):
    """No approval comment kept the locked blobs of that time: only the held body's Goal can release it."""
    ctx = legacy_case(tmp_path, monkeypatch, posted=False)
    approve(ctx, T0 + timedelta(hours=1), post=False, **edit)
    assert (orch_retry_hold.refusal(ctx, 1) is None) is released


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


def run_and_flag(ctx, tmp_path, start, flagged):
    """A run that starts at ``start`` and is held with the same-class flag at ``flagged``."""
    ctx.store._now = lambda: start
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    ctx.store._now = lambda: flagged
    ctx.store.finish_run(1, "blocked")
    actions.flag(SimpleNamespace(**{**vars(ctx), "now": lambda: flagged}), 1, Reason.REVIEW_DEFECTS,
                 "Two failed rounds of the same class; awaiting orchestrator decision.")


def test_an_answer_seconds_before_the_flag_of_its_run_allows_retry(held, tmp_path):
    """The hold's request answered while its run was still flagging releases it (#2868)."""
    from foragent import orch_inbox, review_scoring

    held.store._now = lambda: T0 + timedelta(minutes=10)
    request = orch_inbox.add_request(held.store, "gate", "Review hold for #1",
                                     {"kind": review_scoring.REQUEST_KIND, "issue": 1})
    run_start = T0 + timedelta(minutes=9)
    held.store._now = lambda: T0 + timedelta(minutes=11)
    orch_inbox.set_request_status(held.store, request["id"], "done", answer="ship with follow-up",
                                  require_answer=True)
    run_and_flag(held, tmp_path, run_start, T0 + timedelta(minutes=11, seconds=5))
    assert orch_retry_hold.refusal(held, 1) is None


def test_an_answer_before_a_later_run_does_not_release_that_runs_hold(held, tmp_path):
    """A retry released by the answer starts a new run; a hold it meets without a new request stays."""
    decide(held, "ship with follow-up", minutes=10)
    run_and_flag(held, tmp_path, T0 + timedelta(minutes=20), T0 + timedelta(minutes=30))
    assert orch_retry_hold.refusal(held, 1) is not None
