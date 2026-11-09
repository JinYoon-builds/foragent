"""Acceptance tests for view-reads-owner-records: the lane view shows owners' records.

Statements:
1. The startable count and the finish estimate in the orchestrator bubble (added by #2824)
   count exactly the work admission records as startable, and leave out approved work
   admission holds. The startable count is the bubble snapshot's `summary.startable` (#2824's
   published contract in now.json, republished by `fa gate watch --once`); the hero box's
   'N wait their turn' sentence follows `fa gate status` and is out of scope here.
2. A lane's member card shows the executor and model the runner would use for that lane,
   including global and role defaults, not only the lane's own overrides.

Worlds: the shared offline case (fake tracker, local state store, fake clock) after real gate
ticks. The lane view is read only through public entry points: the printed `fa gate watch
--once` screen (the bubble's finish-estimate sentence), the gate's dashboard snapshot it
republishes (now.json, the bubble's startable count) and the
squirrel's member card, opened by a click through the public render_frame/handle_mouse pair.
Admission's own answer is read through `fa orch facts --json` (its admission replay).
Regression controls that already pass on main live in test_view_owner_records_guard.py (including
the merge-owner merge count, which main already shows and which is no longer part of this issue).
"""

import os
import time

import pytest

from member_card_support import card_text, dashboard_card
from owner_records_support import (
    HELD, HELD_KINDS, STARTABLE, admission_startable, admission_world, finish_sentences, gate_queued,
    next_tick, published, screen, startable_count,
)
from test_local_truth import record_gh


@pytest.fixture(autouse=True)
def utf8_terminal(monkeypatch):
    """The bubble is drawn with its rounded box (a UTF-8 terminal, not the ASCII fallback)."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


@pytest.fixture(autouse=True)
def restored_zone():
    old = os.environ.get("TZ")
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


# ---------------------------------------------------------------- 1. startable count and finish estimate


@pytest.mark.parametrize("held", HELD_KINDS)
def test_startable_count_leaves_out_work_admission_holds(git_history, tmp_path, monkeypatch, capsys, held):
    case, main = admission_world(git_history, tmp_path, monkeypatch, held=held, name="held")
    startable, work = admission_startable(case, main, capsys)
    # Fixture guard: admission records only #2 as startable; #3 is approved and held.
    assert startable == {STARTABLE}, f"admission must hold #{HELD} ({held}) and start #{STARTABLE}: {work}"
    summary, _ = published(case, main, capsys)
    count = startable_count(summary)
    assert count == len(startable), (
        f"the bubble's startable count must be admission's record ({len(startable)}: #{STARTABLE}), "
        f"leaving out approved #{HELD} that admission holds ({held}); found {count}: {summary}")


@pytest.mark.parametrize("held", [kind for kind in HELD_KINDS if kind != "parked"])
def test_finish_estimate_leaves_out_work_admission_holds(git_history, tmp_path, monkeypatch, capsys, held):
    """(Parked work is already left out on main; its control is in the guard file.)"""
    alone, alone_main = admission_world(git_history, tmp_path, monkeypatch, name="alone")
    alone_screen = screen(alone, alone_main, capsys)
    case, main = admission_world(git_history, tmp_path, monkeypatch, held=held, name="held")
    startable, work = admission_startable(case, main, capsys)
    assert startable == {STARTABLE}, f"admission must hold #{HELD} ({held}) and start #{STARTABLE}: {work}"
    shown = screen(case, main, capsys)
    expected = finish_sentences(alone_screen)
    assert expected, f"fixture guard: one startable size:l issue has a finish estimate:\n{alone_screen}"
    found = finish_sentences(shown)
    assert found == expected, (
        f"the finish estimate must count only admission's startable work (#{STARTABLE}), as in the "
        f"same world without #{HELD}: expected {expected}, found {found}:\n{shown}")


def test_startable_count_differs_from_the_label_count_when_admission_holds_approved_work(
        git_history, tmp_path, monkeypatch, capsys):
    """Same snapshot: the bubble follows admission's record, not the queued labels status counts."""
    case, main = admission_world(git_history, tmp_path, monkeypatch, held="dependency", name="world")
    startable, _ = admission_startable(case, main, capsys)
    queued = gate_queued(case, main, capsys)
    # Fixture guard: the label-derived count still includes the held issue.
    assert queued == 2 and startable == {STARTABLE}, (queued, startable)
    for width in (80, 120, 200):
        summary, shown = published(case, main, capsys, width=width)
        assert startable_count(summary) == 1, (
            f"COLUMNS={width}: one issue is startable, whatever the labels say (queued {queued}): "
            f"{summary}\n{shown}")


def test_startable_count_and_finish_estimate_follow_admission_when_it_releases_the_hold(
        git_history, tmp_path, monkeypatch, capsys):
    """On the next snapshot after admission stops holding #3, both values count it."""
    case, main = admission_world(git_history, tmp_path, monkeypatch, held="dependency", name="world")
    before, before_screen = published(case, main, capsys)
    case.tracker.close_issue(4)
    next_tick(case)
    startable, work = admission_startable(case, main, capsys)
    assert startable == {STARTABLE, HELD}, f"fixture guard: closing #4 releases #3: {work}"
    after, after_screen = published(case, main, capsys)
    assert finish_sentences(before_screen) and finish_sentences(after_screen) != finish_sentences(before_screen), (
        f"releasing #{HELD} adds its work to the finish estimate:\n{before_screen}\n{after_screen}")
    assert (startable_count(before), startable_count(after)) == (1, 2), (before, after)


# ---------------------------------------------------------------- 2. executor and model on the member card

GLOBAL_EXECUTOR, GLOBAL_MODEL = "codex", "gpt-walrus-42"
PINNED_EXECUTOR, PINNED_MODEL = "claude", "opus-pinned-7"


def _card_world(git_history, tmp_path, monkeypatch):
    """`plain` sets no executor or model of its own; `pinned` sets both; the defaults name others."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    lanes = {"plain": {"paths": ["plain/**"]},
             "pinned": {"paths": ["pinned/**"], "executor": PINNED_EXECUTOR, "model": PINNED_MODEL}}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language="en", skill_libraries=[],
                     executor=GLOBAL_EXECUTOR, executor_model=GLOBAL_MODEL)
    main = bind_cli(case, monkeypatch)
    tick(tick_context(case, start_runs=False))
    return case, main


def _runner_choice(config, lane):
    """What the runner would launch for the lane: the runner's own lane configuration."""
    from foragent.run_implementation import lane_config

    effective = lane_config(config, lane)
    return effective.executor, effective.executor_model


def test_member_card_shows_the_default_executor_and_model_for_a_lane_without_overrides(
        git_history, tmp_path, monkeypatch, capsys):
    case, main = _card_world(git_history, tmp_path, monkeypatch)
    executor, model = _runner_choice(case.ctx.config, "plain")
    # Fixture guard: the runner uses the defaults for this lane.
    assert (executor, model) == (GLOBAL_EXECUTOR, GLOBAL_MODEL)
    gh = record_gh(monkeypatch)
    _, _, rows, _ = dashboard_card(case, main, capsys, "plain")
    assert gh == []
    card = card_text(rows)
    assert GLOBAL_MODEL in card and GLOBAL_EXECUTOR in card.lower(), (
        f"the plain lane's card shows the executor ({executor}) and model ({model}) the runner would "
        f"use, from the defaults, not an empty or unknown value for missing lane overrides:\n{card}")
    assert card.count(GLOBAL_MODEL) == 1, f"the model is shown once:\n{card}"
    assert PINNED_MODEL not in card and PINNED_EXECUTOR not in card.lower(), (
        f"another lane's overrides do not leak into the card:\n{card}")


def test_member_card_lane_overrides_still_win_over_the_defaults(git_history, tmp_path, monkeypatch, capsys):
    case, main = _card_world(git_history, tmp_path, monkeypatch)
    assert _runner_choice(case.ctx.config, "pinned") == (PINNED_EXECUTOR, PINNED_MODEL)
    _, _, rows, _ = dashboard_card(case, main, capsys, "pinned")
    card = card_text(rows)
    assert PINNED_MODEL in card and PINNED_EXECUTOR in card.lower(), (
        f"the pinned lane's card shows its own executor and model:\n{card}")
    assert GLOBAL_MODEL not in card, f"a lane override replaces the default model:\n{card}"
