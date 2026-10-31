"""Locked: a published PR owns its current try instructions."""
from dataclasses import replace
from datetime import UTC, datetime

from foragent.config import Config
from foragent.inspect_data import InspectContext, collect
from foragent.state import StateStore
from foragent.tracker import FakeTracker


def test_inspect_prefers_current_pr_over_stale_local_try_copy(tmp_path):
    """Reject a saved pre-publication card silently overriding a later edit to the published PR."""
    now = datetime(2026, 10, 31, 1, tzinfo=UTC)
    tracker = FakeTracker(now=lambda: now)
    tracker.add_issue(7, 'Receipt feature')
    card = '## What changed\nCurrent receipt behavior.\n## Try it\n```sh\nfa receipt preview\n```\n## Expect\nThe chosen locale appears.\n'
    pr = tracker.create_pr(head='foragent/issue-7', base='main', title='Receipts', body='Closes #7\n## Try it\n' + card)
    tracker.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
    store = StateStore(tmp_path / 'state', now=lambda: now)
    saved = store.root / 'try/7.md'
    saved.parent.mkdir(parents=True)
    saved.write_text(card.replace('Current receipt behavior.', 'Obsolete draft behavior.'))
    ctx = InspectContext(tracker, store, 'owner/repo', Config(), lambda: now)
    item, = [item for item in collect(ctx).items if item.group == 'try']
    assert item.try_card.what_changed == 'Current receipt behavior.', 'A local draft overrode the published card'
    tracker.update_pr(pr, base='main', title='Receipts', body='Closes #7\n## Try it\n' + card.replace('Current receipt behavior.', 'Edited published behavior.'))
    item, = [item for item in collect(ctx).items if item.group == 'try']
    assert item.try_card.what_changed == 'Edited published behavior.'
