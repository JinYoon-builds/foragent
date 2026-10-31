"""`fa orch dream --redo` (#1231): the redo event makes only its lane due until a fresh dream completes.

The command records the redo; the tick is the only closer of a stale dream PR, so a PR opened from a
prompt built before the redo is closed however late it appears.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_dream, scheduler
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError

NOW = datetime(2026, 10, 31, 9, tzinfo=UTC)
BEFORE = NOW - timedelta(hours=1)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    config = Config(repo="o/r", lanes={"alpha": {"paths": ["a/**"]}, "beta": {"paths": ["b/**"]}})
    tracker = FakeTracker(now=lambda: clock[0])
    closed, starts = [], []
    original = tracker.close_pr

    def close(number, comment=None):
        closed.append(number)
        return original(number, comment)

    tracker.close_pr = close
    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "_quota_exhausted", lambda store, now: False)
    ctx = SimpleNamespace(store=store, config=config, tracker=tracker, now=lambda: clock[0], clock=clock,
                          closed=closed, starts=starts)

    def launch(**kwargs):  # A fresh dream: its prompt is built now, after the redo.
        starts.append(kwargs)
        store.finish_run(f"dream-{kwargs['lane']}", "unchanged")
        orch_dream.record_done(store, kwargs["lane"], 0, "en", clock[0])

    ctx.launcher = launch
    return ctx


def tick(ctx, lanes=("alpha",)):
    report = SimpleNamespace(actions=[])
    due = orch_dream.dream_due(ctx.store, ctx.config, ctx.tracker, ctx.now())
    orch_dream.start(ctx, [lane for lane in lanes if lane in due], 2, report)
    return report.actions


def old_pr(ctx, lane="alpha", covered=BEFORE, publish=True):
    pr = ctx.tracker.create_pr(head=f"foragent/dream-{lane}", base="main", title="Dream", body="Old")
    if publish:
        orch_dream.record_published(ctx.store, lane, 1, covered, covered_until=covered, pr=pr,
                                    inputs={}, outputs={})
    return pr


def test_redo_makes_only_its_lane_due_without_merges_until_a_dream_completes(ctx, capsys):
    assert orch_dream.dream_due(ctx.store, ctx.config, ctx.tracker, NOW) == []
    assert orch_dream.redo(ctx, "alpha") == 0
    assert "redo recorded" in capsys.readouterr().out
    assert orch_dream.dream_due(ctx.store, ctx.config, ctx.tracker, NOW) == ["alpha"]
    orch_dream.record_done(ctx.store, "alpha", 0, "en", NOW)
    assert orch_dream.dream_due(ctx.store, ctx.config, ctx.tracker, NOW) == []


def test_command_writes_no_github_and_unconfigured_lane_records_nothing(ctx, capsys):
    pr = old_pr(ctx)
    assert orch_dream.redo(ctx, "gamma") == 2
    assert "not configured" in capsys.readouterr().out
    assert orch_dream.redo(ctx, "alpha") == 0
    assert ctx.closed == [] and ctx.tracker.get_issue(pr).state == "open", "Only the tick closes"
    assert [event["kind"] for event in orch_dream._lane_events(ctx.store, "gamma")] == []


def test_tick_closes_only_the_stale_pr_then_starts_once(ctx):
    pr, other = old_pr(ctx), old_pr(ctx, "beta")
    orch_dream.redo(ctx, "alpha")
    actions = tick(ctx, ("alpha", "beta"))
    assert ctx.closed == [pr] and ctx.tracker.get_issue(other).state == "open"
    assert f"lane alpha dream PR #{pr} closed for redo; a fresh dream follows" in actions
    assert ctx.starts == [{"kind": "dream", "lane": "alpha"}]
    tick(ctx)
    assert ctx.closed == [pr] and len(ctx.starts) == 1


def test_pr_published_after_the_redo_from_an_older_prompt_is_closed(ctx):
    """Review #1231: a worker of unknown liveness publishes after the redo (or while it is recorded)."""
    orch_dream.redo(ctx, "alpha")
    ctx.clock[0] = NOW + timedelta(minutes=10)
    pr = old_pr(ctx, covered=BEFORE)  # Published now, from a prompt built before the redo.
    tick(ctx)
    assert ctx.closed == [pr] and ctx.starts == [{"kind": "dream", "lane": "alpha"}]


def test_open_pr_without_a_recorded_publication_is_stale_for_a_redo(ctx):
    pr = old_pr(ctx, publish=False)  # Its worker died between opening the PR and recording it.
    orch_dream.redo(ctx, "alpha")
    tick(ctx)
    assert ctx.closed == [pr] and len(ctx.starts) == 1


def test_fresh_dream_pr_after_the_redo_is_kept_and_merge_is_allowed(ctx):
    orch_dream.redo(ctx, "alpha")
    pr = old_pr(ctx, covered=NOW + timedelta(minutes=1))
    assert not orch_dream.stale_for_redo(ctx.store, "alpha", pr)
    actions = tick(ctx)
    assert ctx.closed == [] and ctx.starts == []
    assert f"lane alpha dream PR #{pr} is open; the lane does not dream" in actions


def test_merge_gate_predicate_refuses_a_stale_pr_only_while_a_redo_stands(ctx):
    pr = old_pr(ctx)
    assert not orch_dream.stale_for_redo(ctx.store, "alpha", pr)
    orch_dream.redo(ctx, "alpha")
    assert orch_dream.stale_for_redo(ctx.store, "alpha", pr)
    assert not orch_dream.stale_for_redo(ctx.store, "beta", pr)


def test_close_failure_keeps_the_pr_and_the_next_tick_retries(ctx):
    pr = old_pr(ctx)
    orch_dream.redo(ctx, "alpha")
    working = ctx.tracker.close_pr

    def fail(number, comment=None):
        raise TrackerError("connection reset")

    ctx.tracker.close_pr = fail
    actions = tick(ctx)
    assert any("not closed for redo" in action for action in actions)
    assert ctx.tracker.get_issue(pr).state == "open" and ctx.starts == []
    ctx.tracker.close_pr = working
    tick(ctx)
    assert ctx.closed == [pr] and len(ctx.starts) == 1


def test_old_dream_pr_merged_before_redo_does_not_answer_the_redo(ctx):
    """The tick reconciles the old merged PR, then still starts the fresh dream once."""
    pr = old_pr(ctx)
    ctx.tracker.merge_pr(pr, expected_head_sha=ctx.tracker.pr_head_sha(pr))  # Not reconciled yet.
    orch_dream.redo(ctx, "alpha")
    tick(ctx)
    done = [event for event in orch_dream._lane_events(ctx.store, "alpha") if event["kind"] == orch_dream.DONE]
    assert [event.get("pr") for event in done] == [pr, None], "old PR reconciled, then the fresh dream completed"
    assert ctx.starts == [{"kind": "dream", "lane": "alpha"}]
    assert orch_dream.dream_due(ctx.store, ctx.config, ctx.tracker, NOW) == []


def test_completion_of_a_dream_started_before_the_redo_leaves_the_lane_due(ctx):
    orch_dream.redo(ctx, "alpha")
    orch_dream.record_done(ctx.store, "alpha", 1, "en", NOW + timedelta(minutes=5), covered_until=BEFORE)
    assert orch_dream.dream_due(ctx.store, ctx.config, ctx.tracker, NOW) == ["alpha"]
