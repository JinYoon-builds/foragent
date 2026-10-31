"""Locked: first build reads the current parent; that build then keeps its contract."""
from datetime import timedelta

from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home  # noqa: F401

OLD = 'Use the original receipt constraints.'
NEW = 'Use the revised receipt constraints.'


def attach_parent(case, text):
    case.tracker.add_issue(90, 'Shared outcome', text + '\n<!-- foragent:plan id=receipts map={"child":1} acceptance={} -->')
    case.tracker.set_parent(1, 90)


def test_unstarted_child_refreshes_parent_even_if_approval_copy_already_has_old_text(
    git_history, tmp_path, monkeypatch,
):
    """Reject keeping parent text for the whole approval rather than for a build that started."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command='true')
    attach_parent(case, NEW)
    # A retained approval projection predates this build and is not the current parent.
    approval = case.store.load_approval(1)
    case.store.freeze_parent_context(1, approval, OLD)
    case.implementer.steps = [case.implement]
    assert run_issue(1, case.ctx) == 'merged'
    prompt = case.implementer.calls[0]['prompt']
    assert NEW in prompt, 'A child not yet built must read the parent current when its build starts'
    assert OLD not in prompt


def test_started_child_keeps_parent_and_publishes_reconstructible_contract(
    git_history, tmp_path, monkeypatch,
):
    """Reject refreshing a running build, or leaving its only full contract in local approval state."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command='true')
    attach_parent(case, OLD)
    approved_body = case.tracker.get_issue(1).body

    def first(kwargs):
        assert OLD in kwargs['prompt']
        case.clock[0] += timedelta(seconds=1)
        parent = case.tracker.get_issue(90)
        case.tracker.update_issue(90, title=parent.title, body=parent.body.replace(OLD, NEW))
        (kwargs['worktree'] / 'draft.txt').write_text('unfinished')

    def second(kwargs):
        assert OLD in kwargs['prompt'] and NEW not in kwargs['prompt'], 'A running child must finish against its first parent'
        return case.implement(kwargs)

    case.implementer.steps = [first, second]
    assert run_issue(1, case.ctx) == 'merged'
    assert len(case.implementer.calls) == 2
    public = '\n'.join(c.body for c in case.tracker.list_comments(1)) + case.tracker.get_issue(1).body
    assert OLD in public, 'The original parent contract must survive loss of local approvals'
    assert approved_body in public, 'Public contract evidence must preserve the approved child body'
    approval = case.store.load_approval(1)
    for value in (approval.body_sha, approval.approved_by, approval.approved_at, *approval.acceptance_blobs.values()):
        assert value in public, 'Public execution evidence must identify its approval generation and locked checks'
