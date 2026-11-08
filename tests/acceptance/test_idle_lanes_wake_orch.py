"""Acceptance tests for idle-lanes-wake-orch (PM r1211, 11-08).

From 05:07 to 06:09 on 11-08 every lane was idle while issues waited for approval, and
nothing told the orchestrator until the PM asked by hand. After this issue the gate tick
puts one request in the orchestrator inbox that names the waiting issues, once per idle
stretch.

Only public entry points are driven: the real ``fa gate tick`` (without ``--no-start``,
the way the installed gate job runs), ``fa orch request-status`` or ``fa orch signal-answer`` (whichever fits the request type) for the orchestrator's
answer, and ``foragent.orch_inbox.requests`` (every request in any status) for what the
orchestrator received. Issues live in the offline FakeTracker that the tick reads like
GitHub; a busy lane is a recorded run (no process is started, probed or signalled).

Observable contract chosen here (field names and wording stay the lane's choice):

- "A ready issue waits for approval": an open issue with a ``lane:`` label and a Done when
  section, no ``approved`` label and no hold (the gate's existing ``unapproved_ready`` rule
  in ``orch_facts_stall``).
- "Every lane is idle": no configured lane (and not ``main``) has a live run.
- "Names the waiting issues": every waiting issue number appears in the request's text or
  facts (as ``#N`` or a bare number). The numbers 741 and 863 are chosen so that they do
  not occur in times or counts.
- "Adds none": the count of requests that name any waiting issue does not grow, even after
  the orchestrator closes the first request.

Break-it: no request (skipped effect), a request per tick or two in one tick (doubled
effect), a request that leaves out a waiting issue or waits for a 30-minute hold (wrong
value), and a request that repeats after the orchestrator answered it each fail a check.
"""

from datetime import timedelta
import json
import re

import pytest

from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)
from test_flow_stall_signals import configure, unapproved


LANES = ("kestrel", "heron")
WAITING = (741, 863)


@pytest.fixture
def idle_case(tick_case):  # noqa: F811
    case = tick_case
    configure(case, LANES, roles=None)
    return case


def tick(case, capsys, minutes=5):
    """One real ``fa gate tick`` ``minutes`` after the last, in a fresh store (a new process)."""
    from foragent.state import StateStore

    case.clock[0] += timedelta(minutes=minutes)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for number in getattr(case, "busy", []):
        case.store.update_run(number, last_progress_at=case.clock[0].isoformat())
    capsys.readouterr()
    code = cli_exit(case.main, ["gate", "tick", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa gate tick failed: {output.err}"


def names(row, number) -> bool:
    text = json.dumps({key: row.get(key) for key in ("text", "facts", "signal", "issues", "issue")},
                      ensure_ascii=False, default=str)
    return re.search(rf"(?<![\w.:-]){number}(?![\w.:-])", text) is not None


def naming_requests(case, numbers=WAITING):
    """Every inbox request, in any status, that names any of ``numbers``."""
    from foragent import orch_inbox

    return [row for row in orch_inbox.requests(case.store) if any(names(row, n) for n in numbers)]


def assert_one_request(case, numbers=WAITING, *, when):
    found = naming_requests(case, numbers)
    assert found, (f"{when}: every lane is idle while issues {list(numbers)} wait for approval, so the "
                   "gate tick must add one orchestrator inbox request naming them; none was found")
    assert len(found) == 1, (f"{when}: one idle stretch must add exactly one request naming the waiting "
                             f"issues, found {len(found)}: {[row.get('text') for row in found]}")
    request = found[0]
    missing = [n for n in numbers if not names(request, n)]
    assert not missing, (f"{when}: the request must name every waiting issue; it leaves out {missing}: "
                         f"{request.get('text')!r}")
    return request


ANSWER_COMMANDS = (
    # A plain request is closed by `fa orch request-status`; a gate role-signal request refuses
    # that and is answered by `fa orch signal-answer` (called, or not-now with a reason).
    ["orch", "request-status", "{id}", "done", "--answer", "Preread"],
    ["orch", "signal-answer", "{id}", "called", "--why", "Preread the waiting issues"],
    ["orch", "signal-answer", "{id}", "not-now", "--why", "Preread the waiting issues"],
)


def answer(case, capsys, request):
    """The orchestrator answers the request through whichever public answer command fits its type."""
    tried = []
    for command in ANSWER_COMMANDS:
        argv = [part.replace("{id}", request["id"]) for part in command] + ["--repo-root", str(case.root)]
        capsys.readouterr()
        code = cli_exit(case.main, argv)
        output = capsys.readouterr()
        if code == 0:
            return
        tried.append(f"{' '.join(argv[:4])} -> {code}: {output.err.strip()[:200]}")
    pytest.fail("The orchestrator could not answer the request with request-status or signal-answer: "
                + "; ".join(tried))


def busy(case, number, lane):
    """One live run in ``lane``: an approved issue with a recorded run and the running label."""
    from foragent.issue import body_hash
    from test_flow_stall_signals import BODY

    case.tracker.add_issue(number, f"Running work {number}", BODY, labels=["approved", "running", f"lane:{lane}"])
    case.store.save_approval(number, body=BODY, body_sha=body_hash(BODY), acceptance_blobs={},
                             approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number),
                         lane=lane)
    case.busy = [*getattr(case, "busy", []), number]


def finish(case, number):
    """The run ends and its issue closes: its lane is idle again."""
    case.store.finish_run(number, outcome="cancelled")
    case.tracker.remove_labels(number, ["running"])
    case.tracker.close_issue(number)
    case.busy = [n for n in case.busy if n != number]


def test_idle_lanes_with_waiting_approvals_add_one_request_per_stretch(idle_case, capsys):
    case = idle_case
    for number in WAITING:
        unapproved(case, number, LANES[0])

    tick(case, capsys)
    request = assert_one_request(case, when="first tick of the idle stretch")

    # The orchestrator reads and answers it; the stretch goes on past any 30-minute hold.
    answer(case, capsys, request)
    for _ in range(8):
        tick(case, capsys)
    assert_one_request(case, when="later ticks of the same idle stretch")


def test_a_new_idle_stretch_adds_one_new_request(idle_case, capsys):
    case = idle_case
    unapproved(case, WAITING[0], LANES[0])
    busy(case, 900, LANES[1])

    tick(case, capsys)
    tick(case, capsys)
    assert naming_requests(case, WAITING[:1]) == [], (
        "While a lane has a live run, not every lane is idle: no request may name the waiting issue")

    finish(case, 900)
    tick(case, capsys)
    assert_one_request(case, WAITING[:1], when="first tick after the last run ended")
    tick(case, capsys)
    tick(case, capsys)
    assert_one_request(case, WAITING[:1], when="later ticks of that idle stretch")

    # A lane works again, then goes idle again: a new stretch, one new request.
    busy(case, 901, LANES[1])
    tick(case, capsys)
    assert len(naming_requests(case, WAITING[:1])) == 1, "A busy lane must not add a request"
    finish(case, 901)
    tick(case, capsys)
    tick(case, capsys)
    found = naming_requests(case, WAITING[:1])
    assert len(found) == 2, (
        f"A new idle stretch must add exactly one new request naming the waiting issue; found {len(found)} in all")
