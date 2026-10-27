"""Acceptance test for scorecard-removed-approval. Locked by Foragent; do not edit."""

from datetime import timedelta

import pytest

from test_scorecard_flow import NOW, _approved, _budget, _card, _metric, flow  # noqa: F401 (fixture)

# Evidence (2026-10-27): the real scorecard showed 1416 idle minutes in a day with ~8 active
# runs, because #129, #170 and #173 keep an approval snapshot but lost the approved label
# with no recorded label event, so each counted as startable all day. The bar (D96, the
# scorecard is a diagnostic): an open, not running issue whose current labels lack approved is not
# startable at any moment of the window, even without a recorded time for the change.


def test_an_approval_snapshot_without_a_current_approved_label_is_never_idle_work(flow, capsys):  # noqa: F811
    start = NOW - timedelta(hours=1)
    _budget(flow, start)
    # Approved a day ago (snapshot and labels); the labels were later removed with no
    # label event recorded, and the local snapshot was refreshed 10 minutes ago.
    _approved(flow, 10, start - timedelta(days=1))
    flow.store.record_read(10, "label_events", [], baseline=flow.store.read_baseline())
    flow.clock[0] = NOW - timedelta(minutes=10)
    flow.store.update_local_issue(10, labels=[], state="open", blocked_by=[])
    assert _metric(_card(flow, capsys), "idle_minutes") == pytest.approx(0), (
        "A removed approval must not count as startable work")


def test_a_currently_approved_issue_still_counts(flow, capsys):  # noqa: F811
    start = NOW - timedelta(hours=1)
    _budget(flow, start)
    _approved(flow, 10, start - timedelta(days=1))
    flow.store.record_read(10, "label_events", [], baseline=flow.store.read_baseline())
    flow.clock[0] = NOW - timedelta(minutes=10)
    flow.store.update_local_issue(10, labels=[], state="open", blocked_by=[])
    _approved(flow, 11, start)
    assert _metric(_card(flow, capsys), "idle_minutes") == pytest.approx(60), (
        "Only the issue that is approved now counts, from its approval")
