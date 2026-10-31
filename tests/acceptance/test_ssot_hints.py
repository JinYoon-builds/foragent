"""Locked: retry and talk cannot destroy each other's undelivered advice."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event, current_thread

from foragent.runner import run_issue
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_talk_state import talk_case


def test_retry_and_talk_race_preserves_both_messages_until_delivery(git_history, tmp_path, monkeypatch):
    """Reject retry overwriting talk, and acknowledgement removing a message not in that prompt.

    Pause a filesystem write, not the queue algorithm: a serialized implementation
    may block the other producer, and is released without requiring it to finish.
    """
    case, main, commands, gh_calls = talk_case(git_history, tmp_path, monkeypatch, executor='claude', state='blocked', worktree=False)
    retry_hint = 'Use the selected locale'
    talk_hint = 'Preserve the receipt timezone'
    reached, release, other_finished = Event(), Event(), Event()
    original = os.replace
    paused = [False]

    def delayed(source, target, *args, **kwargs):
        if (current_thread().name.startswith('retry-producer') and
                str(target).endswith('/hints/1.txt') and not paused[0]):
            paused[0] = True
            reached.set()
            assert release.wait(10), 'Test failed to release the first producer'
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(os, 'replace', delayed)

    def retry():
        from foragent import orch
        return orch.retry(case.ctx, 1, hint=retry_hint)

    def talk():
        try:
            case.store.start_run(1, branch='foragent/issue-1', worktree=case.store.worktree_path(1))
            case.tracker.add_labels(1, ['running'])
            return cli_exit(main, ['lanes', 'talk', '1', '--repo-root', str(case.root), '--message', talk_hint])
        finally:
            other_finished.set()

    with ThreadPoolExecutor(1, thread_name_prefix='retry-producer') as first:
        retry_result = first.submit(retry)
        # Implementations using a different backing store need not hit this I/O seam.
        if reached.wait(3):
            with ThreadPoolExecutor(1) as second:
                talk_result = second.submit(talk)
                other_finished.wait(2)
                release.set()
                assert talk_result.result(timeout=10) == 0
        else:
            release.set()
            retry_result.result(timeout=10)
            assert talk() == 0
        retry_result.result(timeout=10)
    case.store.finish_run(1, 'cancelled')
    case.store.start_run(1, branch='foragent/issue-1', worktree=case.store.worktree_path(1))
    late = 'Keep the second receipt field'

    def first_turn(kwargs):
        assert cli_exit(main, ['lanes', 'talk', '1', '--repo-root', str(case.root), '--message', late]) == 0
        (kwargs['worktree'] / 'draft.txt').write_text('unfinished')

    case.implementer.steps = [first_turn, case.implement]
    assert run_issue(1, case.ctx) == 'merged'
    prompts = [call['prompt'] for call in case.implementer.calls]
    assert retry_hint in prompts[0] and talk_hint in prompts[0], 'Concurrent producers lost undelivered advice'
    public = case.tracker.get_issue(1).body + '\n'.join(c.body for c in case.tracker.list_comments(1))
    assert retry_hint in public and talk_hint in public, 'Both accepted messages need durable issue-owned evidence'
    assert late in prompts[1], 'Acknowledging the first prompt erased later advice'
    assert retry_hint not in prompts[1] and talk_hint not in prompts[1], 'Delivered advice was sent twice'
    assert gh_calls == []
