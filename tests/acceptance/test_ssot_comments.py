"""Locked: state changes get one durable line; unchanged state gets no comment."""
from foragent.runner import run_issue
from test_orch_verbs import blocked_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def test_retry_hint_is_one_line_and_unchanged_retry_does_not_repeat_comment(git_history, tmp_path, monkeypatch):
    """Reject multiline chatter and emitting the same retry transition on every invocation."""
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    before = len(case.tracker.list_comments(1))
    hint = 'Use the selected locale'
    assert cli_exit(main, ['orch', 'retry', '1', '--hint', hint]) == 0
    added = case.tracker.list_comments(1)[before:]
    assert len(added) == 1 and len(added[0].body.splitlines()) == 1, 'A retry transition owes exactly one comment line'
    assert hint in added[0].body
    assert cli_exit(main, ['orch', 'retry', '1', '--hint', hint]) == 0
    assert case.tracker.list_comments(1)[before:] == added, 'An unchanged retry must not comment twice'


def test_blocked_reason_is_one_line_with_full_evidence_reachable(git_history, tmp_path, monkeypatch):
    """Reject a multiline blocked announcement or one-line truncation that loses its evidence."""
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    assert cli_exit(main, ['orch', 'retry', '1']) == 0
    case.store.start_run(1, branch='foragent/issue-1', worktree=case.store.worktree_path(1))
    question = 'Choose the receipt locale\nA: English\nB: Korean\n'

    def ask(kwargs):
        path = kwargs['worktree'] / '.foragent/QUESTION.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text(question)

    case.implementer.steps = [ask]
    before = len(case.tracker.list_comments(1))
    assert run_issue(1, case.ctx) == 'spec-question'
    added = case.tracker.list_comments(1)[before:]
    blocked = [c for c in added if 'reason=spec-question' in c.body]
    assert len(blocked) == 1 and len(blocked[0].body.splitlines()) == 1, 'A blocked transition owes one line'
    public = case.tracker.get_issue(1).body + '\n'.join(c.body for c in case.tracker.list_comments(1))
    assert all(value in public for value in ('Choose the receipt locale', 'English', 'Korean')), 'One line must retain the question or link its full durable evidence'
