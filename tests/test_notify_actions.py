"""Delivery failures and event identity regressions for the human push policy."""

from dataclasses import replace
import json

import pytest

from foragent.config import ConfigError, load_config
from foragent.inspect_actions import answer, notify
from foragent.inspect_data import collect
from foragent.state import StateStore
from foragent.reasons import Reason
from test_inspect import ctx, flag, human_question, pr  # noqa: F401


def test_partial_delivery_failure_retries_only_the_failed_item_after_restart(ctx, monkeypatch):
    human_question(ctx, 1)
    human_question(ctx, 2)
    sent = []

    def send(config, title, line, link):
        sent.append((line, link))
        return '#1 ' in line

    monkeypatch.setattr('foragent.inspect_actions.notifications.send', send)
    notify(ctx)
    assert len(sent) == 2
    assert ctx.store.load_inspect().notified_items == ['issue:1:comment:1']
    assert ctx.store.load_inspect().notified_signature is None
    ctx.store = StateStore(ctx.store.root)
    monkeypatch.setattr('foragent.inspect_actions.notifications.send',
                        lambda *args: sent.append((args[2], args[3])) or True)
    assert notify(ctx) == '#2 Issue 2 (escalated: Summary 2)'
    assert len(sent) == 3
    assert sent[-1][1] == 'https://github.com/owner/repo/issues/2'
    assert notify(ctx) is None


def test_answer_before_first_observation_never_pushes(ctx):
    human_question(ctx)
    answer(ctx, 1, 'Use the existing scope.')
    assert notify(ctx, lambda *args: pytest.fail('Answered decision pushed')) is None


def test_edits_keep_identity_but_new_question_on_same_issue_pushes(ctx):
    human_question(ctx)
    sent = []
    deliver = lambda line, title: sent.append(line)
    notify(ctx, deliver)
    comment = ctx.tracker.comments[1][-1]
    ctx.tracker.comments[1][-1] = replace(comment, body=comment.body + '\nClarification')
    assert notify(ctx, deliver) is None
    ctx.tracker.comment(1, '<!-- foragent:needs-human reason=escalated -->\nNew choice?')
    notify(ctx, deliver)
    assert len(sent) == 2 and 'New choice?' in sent[-1]


def test_closed_issue_with_open_pr_never_pushes_a_merge_decision(ctx):
    pr(ctx, 1)
    ctx.tracker.close_issue(1)
    assert collect(ctx).items == []
    assert notify(ctx, lambda *args: pytest.fail('Closed issue pushed')) is None


def test_review_label_changes_keep_the_same_merge_decision(ctx):
    flag(ctx, reason=Reason.RISK_HIGH)
    number = pr(ctx, 1)
    sent = []
    notify(ctx, lambda line, title: sent.append(line))
    ctx.tracker.remove_labels(1, ['needs-human'])
    assert notify(ctx, lambda *args: pytest.fail('Same merge decision pushed again')) is None
    assert len(sent) == 1 and f'PR #{number}' in sent[0]


def test_morning_delivery_failure_retries_and_persists_success(ctx, monkeypatch):
    ctx.config = replace(ctx.config, notify_summary_hour=ctx.now().astimezone().hour)
    sent = []
    monkeypatch.setattr('foragent.inspect_actions.notifications.send', lambda *args: False)
    notify(ctx)
    assert ctx.store.load_inspect().summary_date is None
    monkeypatch.setattr('foragent.inspect_actions.notifications.send',
                        lambda *args: sent.append(args) or True)
    notify(ctx)
    ctx.store = StateStore(ctx.store.root)
    assert notify(ctx) is None
    assert len(sent) == 1 and 'Morning summary' in sent[0][1]
    assert ctx.store.load_inspect().summary_date == ctx.now().astimezone().date().isoformat()


def test_old_inspect_state_loads_and_keeps_seen_time(ctx):
    ctx.store.root.mkdir()
    (ctx.store.root / 'inspect.json').write_text(json.dumps({
        'since': ctx.now().isoformat(), 'notified_signature': 'old-signature',
    }))
    human_question(ctx)
    notify(ctx, lambda *args: None)
    state = StateStore(ctx.store.root).load_inspect()
    assert state.since == ctx.now().isoformat()
    assert len(state.notified_items) == 1 and state.summary_date is None


@pytest.mark.parametrize('hour', [-1, 24, True, '8', None, 8.5])
def test_summary_hour_rejects_invalid_configuration(tmp_path, hour):
    import yaml

    (tmp_path / '.foragent.yml').write_text(yaml.safe_dump({'notify_summary_hour': hour}))
    with pytest.raises(ConfigError, match='notify_summary_hour'):
        load_config(tmp_path, global_path=tmp_path / 'missing.yml')


def test_inspect_names_question_merge_and_summary_kinds(ctx, monkeypatch):
    human_question(ctx, 1)
    flag(ctx, number=2, reason=Reason.RISK_HIGH)
    pr(ctx, 2, body="Closes #2")
    ctx.config = replace(ctx.config, notify_summary_hour=ctx.now().astimezone().hour)
    kinds = []
    monkeypatch.setattr('foragent.notify.deliver',
                        lambda config, title, line, link=None, *, kind=None, root=None: kinds.append(kind))
    notify(ctx)
    assert sorted(kinds) == ['merge', 'question', 'summary']
