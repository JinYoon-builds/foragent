"""Accepted advice keeps its issue evidence across concurrent or deferred publication."""
from datetime import timedelta
from threading import Event, current_thread

from foragent import orch_advice
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import TrackerError
from test_orch_review_handoff import tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
import test_ssot_hints as locked
from test_talk_state import talk_case


def test_existing_hint_contract_with_a_label_send_in_flight(git_history, tmp_path, monkeypatch):
    """Reject losing a successfully queued public copy after both producers return.

    Only fake GitHub I/O is delayed. The original locked check runs with every
    assertion intact, including the remote issue/comment check and both prompts.
    """
    original_case = locked.talk_case
    send_entered, talk_finished = Event(), Event()

    def case_with_slow_github(*args, **kwargs):
        case, main, commands, gh_calls = original_case(*args, **kwargs)
        send = case.tracker.add_labels

        def add_labels(number, labels):
            if current_thread().name.startswith('retry-producer') and not send_entered.is_set():
                send_entered.set()
                # A valid implementation may serialize publication. Release the
                # fake transport even when talk waits for this label send.
                talk_finished.wait(2)
            return send(number, labels)

        def observed_main(*args, **kwargs):
            if not talk_finished.is_set():
                assert send_entered.wait(10), 'Retry did not reach its GitHub label send'
                try:
                    return main(*args, **kwargs)
                finally:
                    talk_finished.set()
            return main(*args, **kwargs)

        monkeypatch.setattr(case.tracker, 'add_labels', add_labels)
        return case, observed_main, commands, gh_calls

    monkeypatch.setattr(locked, 'talk_case', case_with_slow_github)
    locked.test_retry_and_talk_race_preserves_both_messages_until_delivery(
        git_history, tmp_path, monkeypatch,
    )
    assert send_entered.is_set() and talk_finished.is_set()


def test_accepted_talk_survives_a_label_change_until_the_gate_delivers_it(
    git_history, tmp_path, monkeypatch,
):
    """Reject treating accepted message evidence as a superseded transition comment."""
    case, main, commands, gh_calls = talk_case(
        git_history, tmp_path, monkeypatch, executor='claude', worktree=False,
    )
    case.ctx.tracker.get_issue(1)
    message = 'Keep the accepted receipt advice'
    post = case.tracker.comment

    def unavailable(*args, **kwargs):
        raise TrackerError('Temporary comment transport failure (HTTP 422)')

    monkeypatch.setattr(case.tracker, 'comment', unavailable)
    case.clock[0] += timedelta(seconds=1)
    assert cli_exit(main, [
        'lanes', 'talk', '1', '--repo-root', str(case.root), '--message', message,
    ]) == 0
    assert message in (case.store.root / 'hints/1.txt').read_text()
    assert any(entry.get('body') == orch_advice.talk_body(message)
               for entry in case.store.github_outbox())
    monkeypatch.setattr(case.tracker, 'comment', post)
    case.clock[0] += timedelta(seconds=1)
    case.tracker.record_label(1, 'needs-human', 'human')
    expected_issue = case.tracker.get_issue(1)

    for _ in range(2):
        context = tick_context(case, start_runs=False)
        context.store = StateStore(case.store.root, now=case.ctx.now)
        tick(context)
        assert case.tracker.get_issue(1) == expected_issue, 'Evidence delivery changed the person\'s issue state'
        assert [comment.body for comment in case.tracker.list_comments(1)].count(
            orch_advice.talk_body(message),
        ) == 1, 'Accepted advice needs one real issue comment after transport recovers'
        assert context.store.github_outbox() == []
    assert gh_calls == []
