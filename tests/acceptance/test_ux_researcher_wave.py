"""Acceptance test for ux-researcher-wave. Locked by Foragent; do not edit.

When several orders finish together, the UX researcher follows all of them in one
session instead of one session per order. Each finished order is still covered exactly
once, and an order that finishes later gets its own pass.
"""

from datetime import timedelta

from test_architect_project_schedule import order_children
from test_board_status_groups import offline_github  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_ux_researcher_calls import mentions, settle, ux_case  # noqa: F401

WAVE = (900, 901, 902, 903)


def finish_orders(case, orders, first_child):
    child = first_child
    for number in orders:
        case.tracker.add_issue(child, f"Child of order {number}", case.tracker.get_issue(1).body, state="closed")
        order_children(case, number, [child])
        child += 1
    return child


def order_turns(turns, orders):
    return [turn for turn in turns if any(mentions(turn, number) for number in orders)]


def test_orders_finished_together_get_one_ux_session_that_covers_each_once(ux_case):
    case = ux_case
    # Order 900 (child #1) is already finished by the fixture; three more finish with it.
    finish_orders(case, WAVE[1:], first_child=10)
    turns = settle(case, ticks=6)
    covering = order_turns(turns, WAVE)
    assert len(covering) == 1, (
        f"Orders that finished together must share one UX researcher session, not {len(covering)}")
    assert all(mentions(covering[0], number) for number in WAVE), (
        "That one session must name every order of the wave")
    case.clock[0] += timedelta(hours=2)
    assert order_turns(settle(case), WAVE) == [], "A covered order is never followed again"


def test_an_order_finishing_after_the_wave_gets_its_own_session(ux_case):
    case = ux_case
    next_child = finish_orders(case, WAVE[1:], first_child=10)
    assert len(order_turns(settle(case, ticks=6), WAVE)) == 1, "The wave is followed in one session"
    case.clock[0] += timedelta(hours=1)
    finish_orders(case, (904,), first_child=next_child)
    later = order_turns(settle(case), WAVE + (904,))
    assert len(later) == 1 and mentions(later[0], 904), "A later finished order gets one session"
    assert not any(mentions(later[0], number) for number in WAVE), (
        "The later session does not repeat orders the wave already covered")
