"""Waiting on a person: a measured fact the architect signal can use.

Only the boundaries are faked: the offline tracker (issue labels), the clock and
the worker launchers of the existing tick fixture. Each step runs the real
fa gate tick, which reads the issue listing; fa orch facts --json, fa orch inbox
--json and fa orch signal-answer are read and called as a person or the
orchestrator would. Nothing seeds a fingerprint, an answer record or a fact.

Chosen public contract: the kind is named ``waiting_on_person``; its numbers
are ``waiting`` (how many open issues carry needs-human), ``oldest_issue``
("#N") and ``oldest_waiting_seconds``.

Break-it: measuring age from the latest observation, counting closed or
unlabelled issues, re-raising on age, comparing counts only, or raising twice
must each fail a check here.
"""

from datetime import timedelta
import json

import pytest

from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401 (fixture)

KIND = "waiting_on_person"
SIGNAL = {"paths": [], "kinds": [KIND]}
WAIT_LABELS = ["blocked", "needs-human"]


@pytest.fixture
def person_case(tick_case, capsys):  # noqa: F811
    case = tick_case
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [SIGNAL]
    write_role_snapshot(case.root, roles)
    for number in (7, 8, 9):
        case.tracker.add_issue(number, f"Work item {number}", "Implement it.", labels=["approved", "lane:roles"])
    # An hour of observations in which nobody waits on a person.
    for _ in range(12):
        run_tick(case, capsys)
    case.added = {}
    return case


def wait_on_person(case, number):
    case.added[number] = case.clock[0]
    case.tracker.add_labels(number, WAIT_LABELS)


def facts(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "facts", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)


def human_facts(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "facts", "--repo-root", str(case.root)]) == 0
    return capsys.readouterr().out


def waiting(case, capsys):
    measured = facts(case, capsys)
    numbers = measured.get("kind_numbers", {}).get(KIND)
    assert (KIND in measured["kinds"]) == bool(numbers), "A present kind and its numbers go together"
    return numbers


def requests(case, capsys):
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert all(row["from"] == "gate" and row["signal"] == SIGNAL for row in rows)
    assert not any(KIND in json.dumps(push) or "architect" in json.dumps(push) for push in case.pushes), (
        "Role signals belong in the orchestrator inbox, never in a push to the person"
    )
    return rows


def tick(case, capsys):
    run_tick(case, capsys)
    return requests(case, capsys)


def answer(case, capsys, request_id, how):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", "signal-answer", request_id, how, "--why",
                                "The architect looked at who waits on a person", "--repo-root", str(case.root)])
    assert code == 0, capsys.readouterr().err
    assert next(row for row in requests(case, capsys) if row["id"] == request_id)["status"] == "done"


def age_bounds(case, number, observed):
    """The wait started between the label change and the first tick that observed it."""
    now = case.clock[0]
    return (now - observed).total_seconds(), (now - case.added[number]).total_seconds()


def test_one_waiting_item_shows_its_age_and_count_in_facts(person_case, capsys):
    case = person_case
    assert waiting(case, capsys) is None, "Nobody waits on a person yet"
    assert "#7" not in human_facts(case, capsys)
    wait_on_person(case, 7)
    run_tick(case, capsys)
    observed = case.clock[0]
    case.clock[0] += timedelta(hours=3)
    run_tick(case, capsys)

    numbers = waiting(case, capsys)
    assert numbers, "An open needs-human issue must make the kind present"
    assert numbers["waiting"] == 1 and numbers["oldest_issue"] == "#7"
    low, high = age_bounds(case, 7, observed)
    assert low <= numbers["oldest_waiting_seconds"] <= high, (
        "The age runs from when the needs-human label was observed, not from the latest observation"
    )
    assert "#7" in human_facts(case, capsys), "The human facts output names the oldest waiting issue"
    rows = requests(case, capsys)
    assert len(rows) == 1 and rows[0]["status"] != "done", "One met signal raises one request"
    assert "#7" in rows[0]["text"], "The request carries the measured numbers"


@pytest.mark.parametrize("how", ["called", "not-now"])
def test_a_second_waiting_item_raises_the_count_and_calls_again(person_case, capsys, how):
    case = person_case
    wait_on_person(case, 7)
    first = tick(case, capsys)
    assert len(first) == 1
    answer(case, capsys, first[0]["id"], how)

    case.clock[0] += timedelta(hours=1)
    wait_on_person(case, 8)
    current = tick(case, capsys)
    numbers = waiting(case, capsys)
    assert numbers["waiting"] == 2 and numbers["oldest_issue"] == "#7", "The oldest stays the earlier item"
    added = [row for row in current if row["id"] != first[0]["id"]]
    assert len(added) == 1 and added[0]["status"] != "done", "More items waiting than at the answer call again once"
    for _ in range(3):
        assert {row["id"] for row in tick(case, capsys)} == {first[0]["id"], added[0]["id"]}, (
            "An open request is never duplicated"
        )


@pytest.mark.parametrize("how", ["called", "not-now"])
def test_the_same_items_growing_older_never_call_again(person_case, capsys, how):
    case = person_case
    wait_on_person(case, 7)
    wait_on_person(case, 8)
    first = tick(case, capsys)
    assert len(first) == 1
    answer(case, capsys, first[0]["id"], how)

    ages = []
    for advance in (timedelta(), timedelta(hours=13), timedelta(days=2)):
        case.clock[0] += advance
        current = tick(case, capsys)
        assert [row["id"] for row in current] == [first[0]["id"]], (
            "Elapsed time alone, across gate restarts, never calls again"
        )
        numbers = waiting(case, capsys)
        assert numbers["waiting"] == 2 and numbers["oldest_issue"] == "#7"
        ages.append(numbers["oldest_waiting_seconds"])
    assert ages[0] < ages[1] < ages[2], "The age keeps growing while the same items wait"
    assert ages[2] - ages[1] >= timedelta(days=2).total_seconds()


@pytest.mark.parametrize("ends", ["label removed", "issue closed"])
def test_an_item_that_stops_waiting_stops_counting(person_case, capsys, ends):
    case = person_case

    def stop(number):
        if ends == "label removed":
            case.tracker.remove_labels(number, WAIT_LABELS)
        else:
            case.tracker.close_issue(number)

    wait_on_person(case, 7)
    first = tick(case, capsys)
    observed = case.clock[0]
    assert len(first) == 1
    answer(case, capsys, first[0]["id"], "called")
    wait_on_person(case, 8)
    raised = tick(case, capsys)
    assert len(raised) == 2
    answer(case, capsys, raised[-1]["id"], "called")
    expected = [row["id"] for row in raised]

    stop(8)
    current = tick(case, capsys)
    numbers = waiting(case, capsys)
    assert numbers["waiting"] == 1 and numbers["oldest_issue"] == "#7"
    low, high = age_bounds(case, 7, observed)
    assert low <= numbers["oldest_waiting_seconds"] <= high
    assert [row["id"] for row in current] == expected, "Fewer items than at the answer stay quiet"

    stop(7)
    current = tick(case, capsys)
    assert waiting(case, capsys) is None, "With nobody waiting the kind is absent"
    assert [row["id"] for row in current] == expected


def test_a_different_oldest_item_calls_again_with_the_same_count(person_case, capsys):
    case = person_case
    wait_on_person(case, 7)
    first = tick(case, capsys)
    assert len(first) == 1
    answer(case, capsys, first[0]["id"], "not-now")

    # Between two ticks one wait ends and another begins: the count never changes.
    case.tracker.remove_labels(7, WAIT_LABELS)
    wait_on_person(case, 9)
    current = tick(case, capsys)
    numbers = waiting(case, capsys)
    assert numbers["waiting"] == 1 and numbers["oldest_issue"] == "#9"
    added = [row for row in current if row["id"] != first[0]["id"]]
    assert len(added) == 1, "A different oldest waiting item calls again even at the same count"
    assert "#9" in added[0]["text"]
