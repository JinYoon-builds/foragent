"""Acceptance tests for merge-stall-alarm (PM r1224, 11-08).

From 06:44 to 10:07 on 11-08 no lane merge landed on main for 3 hours 23 minutes while
members waited in the merge queue (empty related-check groups retired every train), and
nothing raised it. After this issue, when merge-queue members are ready and no merge has
landed on main for 60 minutes, the gate tick puts one request in the orchestrator inbox
that names the ready members and the latest train retire reasons, once per stall. A new
merge ends the stall.

Only public entry points are driven: the real ``fa gate tick`` (without ``--no-start``,
the way the installed gate job runs), ``fa orch request-status`` for the orchestrator's
answer, and ``foragent.orch_inbox.requests`` (every request in any status) for what the
orchestrator received. The merge owner process is not started (its launcher is replaced
by a recorder); no process is started, probed or signalled.

Observable contract chosen here (field names and wording stay the lane's choice):

- "Ready merge-queue members": issues handed to the merge owner the way
  ``merge_owner.enqueue`` leaves them: a finished ``merge-queued`` run, an open PR, a
  ``merge-owner/<n>.json`` job in phase ``waiting`` and a ``ready`` row with
  ``merge_owner: true`` in ``merge-queue.jsonl``.
- "A merge on main": the way the merge owner records one: the issue's owner merge fact
  (``record_owner_merge``), its ``merged`` turn row and queue exit, the closed issue, and
  a new commit on ``origin/main`` at the merge time. Both the store and Git agree, so the
  lane may read either.
- "Train retire reasons": the way ``merge_owner_batch._close`` records a retirement: the
  ``refs/foragent/batch-spent/`` ledger ref (written by ``_record_retirement``) and the
  ``merge-batch`` flow record, both carrying ``reason``. Reasons carry unique words
  ("quartz lantern", "amber kettle") and no digits; the request must contain the latest
  reason's text.
- "Names the ready members": every ready member number appears in the request
  (as ``#N`` or a bare number). 741 and 863 do not occur in times, SHAs or PR numbers.
- 60 minutes: ticks up to 58 minutes after the last merge add nothing; the tick 63
  minutes after it adds the request.
- "Adds none": the count of requests that name any ready member does not grow on later
  ticks of the same stall, even after the orchestrator closes the first request.

Break-it: no request (skipped effect), a request per tick or two in one tick (doubled
effect), a request before 60 minutes, a request that leaves out a member or the latest
retire reason (wrong value), a repeat after the orchestrator answered, and a stall that
never ends on a new merge each fail a check.
"""

from datetime import timedelta
import json
import re

import pytest

from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)
from test_flow_stall_signals import BODY, configure


LANES = ("kestrel", "heron")
MEMBERS = (741, 863)
EARLIER = (512, 517)  # Issues whose merges landed before the stall.


@pytest.fixture
def stall_case(tick_case, monkeypatch):  # noqa: F811
    import yaml

    from foragent import merge_owner, order_publications_process, scheduler

    case = tick_case
    configure(case, LANES, roles=None)
    path = case.root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config.update({"merge_owner": True, "merge_batch": False, "test_command": "uv run pytest -q"})
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    case.owner_starts = []
    # The owner process is not what this issue changes; its launch is only recorded.
    monkeypatch.setattr(merge_owner, "start", lambda ctx, report: case.owner_starts.append(ctx.now()))
    monkeypatch.setattr(order_publications_process, "start", lambda ctx, report: None)
    # Main moves in these tests; a baseline build would launch a process.
    monkeypatch.setattr(scheduler, "prebuild_acceptance_baseline", lambda *args, **kwargs: None)
    case.prs = {}
    return case


# ---------------------------------------------------------------- world helpers


def tick(case, capsys, minutes=5):
    """One real ``fa gate tick`` ``minutes`` after the last, in a fresh store (a new process)."""
    from foragent.state import StateStore

    case.clock[0] += timedelta(minutes=minutes)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    capsys.readouterr()
    code = cli_exit(case.main, ["gate", "tick", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa gate tick failed: {output.err}"


def at(case, minutes):
    """Move the clock forward without a tick."""
    case.clock[0] += timedelta(minutes=minutes)


def _issue(case, number, lane):
    from foragent.issue import body_hash

    case.tracker.add_issue(number, f"Lane work {number}", BODY, labels=["approved", f"lane:{lane}"])
    case.store.save_approval(number, body=BODY, body_sha=body_hash(BODY), acceptance_blobs={},
                             approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number),
                         lane=lane)
    pr = case.tracker.create_pr(head=f"foragent/issue-{number}", base="main",
                                title=f"Lane work {number}", body=f"Closes #{number}")
    head = f"{number:x}".rjust(40, "a")
    case.tracker.set_pr_head_sha(pr, head)
    case.prs[number] = (pr, head)
    return case.store.get_run(number)


def _queue_row(case, number, started_at, event, **fields):
    from foragent import run_merge_turn

    row = {"n": number, "event": event, "started_at": started_at, "target": "main", **fields,
           "at": case.clock[0].isoformat()}
    with case.store._state_lock():
        run_merge_turn._append(case.store, row)


def ready_member(case, number, lane):
    """Hand ``number`` to the merge owner the way ``merge_owner.enqueue`` leaves it."""
    run = _issue(case, number, lane)
    pr, head = case.prs[number]
    base = case.history.tip
    job = {"pr": pr, "branch": f"foragent/issue-{number}", "target": "main", "head": head, "base": base,
           "approval": None, "ready_at": case.clock[0].isoformat(), "comparison": None,
           "authorization": None, "phase": "waiting"}
    (case.store.root / "merge-owner").mkdir(parents=True, exist_ok=True)
    case.store._write_json(case.store.root / "merge-owner" / f"{number}.json",
                           {"started_at": run.started_at, **job})
    publication = {key: value for key, value in job.items() if key != "comparison"}
    _queue_row(case, number, run.started_at, "ready", head_sha=head, merge_owner=True, publication=publication)
    case.store.finish_run(number, outcome="merge-queued")
    case.tracker.remove_labels(number, ["running", "ready"])


def land(case, number, lane=None):
    """A merge lands on main now, recorded the way the merge owner records one."""
    case.last_merge = case.clock[0]
    run = case.store.get_run(number)
    if run is None or run.status == "running" or number not in case.prs:
        run = _issue(case, number, lane or LANES[0])
        case.store.finish_run(number, outcome="merge-queued")
        _queue_row(case, number, run.started_at, "ready", head_sha=case.prs[number][1], merge_owner=True,
                   publication={"pr": case.prs[number][0]})
    pr, head = case.prs[number]
    case.history.update({f"merged/{number}.txt": "landed\n"}, at=case.clock[0],
                        message=f"Lane work {number} (#{pr})")
    _queue_row(case, number, run.started_at, "turn", members=[number], base_sha=case.history.tip, result="merged")
    case.store.record_owner_merge(number, run.started_at, {"pr": pr, "target": "main", "head": head,
                                                          "merged_at": case.clock[0].isoformat()})
    job_path = case.store.root / "merge-owner" / f"{number}.json"
    if job_path.exists():
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job["phase"] = "merged"
        case.store._write_json(job_path, job)
    _queue_row(case, number, run.started_at, "withdrawn")
    case.tracker.close_issue(number)


def retire(case, reason):
    """One merge train retires, recorded the way ``merge_owner_batch._close`` records it."""
    from hashlib import sha256
    from types import SimpleNamespace

    from foragent import merge_owner_batch

    case.retired = getattr(case, "retired", 0) + 1
    base = case.git._call(["rev-parse", "HEAD"]).strip()
    members = [{"n": n, "head": case.prs[n][1], "started_at": case.store.get_run(n).started_at}
               for n in MEMBERS if n in case.prs]
    identity = sha256(f"train {case.retired} {reason}".encode()).hexdigest()
    merge_owner_batch._record_retirement(SimpleNamespace(git=case.git), identity, base, reason=reason,
                                         base=base, passed=False, members=members, refs={}, spent=[])
    case.store.record_flow("merge-batch", members=members, candidate=base, result="unused",
                           used_members=0, reason=reason, ci_minutes=0)


def _text(row) -> str:
    return json.dumps(row, ensure_ascii=False, default=str)


def names(row, number) -> bool:
    return re.search(rf"(?<![\w.:-]){number}(?![\w.:-])", _text(row)) is not None


def naming_requests(case, numbers=MEMBERS):
    """Every inbox request, in any status, that names any of ``numbers``."""
    from foragent import orch_inbox

    return [row for row in orch_inbox.requests(case.store) if any(names(row, n) for n in numbers)]


def assert_none(case, numbers=MEMBERS, *, when, before=0):
    found = naming_requests(case, numbers)
    assert len(found) == before, (
        f"{when}: no new stall request may name the ready members {list(numbers)} yet "
        f"(less than 60 minutes since the last merge); found {[row.get('text') for row in found]}")


def assert_requests(case, count, numbers=MEMBERS, *, when, reason):
    found = naming_requests(case, numbers)
    assert found, (f"{when}: members {list(numbers)} are ready in the merge queue and no merge landed on "
                   "main for 60 minutes, so the gate tick must add one orchestrator inbox request naming "
                   "them; none was found")
    assert len(found) == count, (
        f"{when}: one stall must add exactly one request naming the ready members; expected {count} in all, "
        f"found {len(found)}: {[row.get('text') for row in found]}")
    request = found[-1]
    missing = [n for n in numbers if not names(request, n)]
    assert not missing, (f"{when}: the request must name every ready member; it leaves out {missing}: "
                         f"{request.get('text')!r}")
    assert reason in _text(request), (
        f"{when}: the request must name the latest train retire reason {reason!r}: {_text(request)}")
    return request


def since_merge(case) -> int:
    return int((case.clock[0] - case.last_merge).total_seconds() // 60)


def stalled_world(case):
    """Two earlier merges, the last at 09:02; two members ready; one old retirement.

    The clock then stands at 09:05, so 5-minute ticks fall 3, 8, ... 58 and 63 minutes after
    the last merge: never on the 60-minute line itself.
    """
    at(case, -28)  # 08:32
    land(case, EARLIER[0])
    at(case, 30)  # 09:02: the last merge before the stall.
    land(case, EARLIER[1])
    for number, lane in zip(MEMBERS, LANES):
        ready_member(case, number, lane)
    retire(case, "tip CI red (obsidian ferry)")
    at(case, 3)


# ---------------------------------------------------------------- tests


def test_one_request_per_stall_names_ready_members_and_latest_retire_reason(stall_case, capsys):
    case = stall_case
    stalled_world(case)

    for index in range(11):  # 09:10 .. 10:00: 8 .. 58 minutes since the 09:02 merge.
        tick(case, capsys)
        if index == 5:  # 09:35: the latest train retires.
            retire(case, "related-check group empty (quartz lantern)")
        assert_none(case, when=f"tick {since_merge(case)} minutes after the last merge")

    tick(case, capsys)  # 10:05, 63 minutes since the last merge.
    assert since_merge(case) == 63
    request = assert_requests(case, 1, when="first tick past 60 minutes without a merge",
                              reason="quartz lantern")

    # The orchestrator answers it; the same stall goes on for more than another hour.
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "request-status", request["id"], "done", "--answer", "Looked at the queue",
                                "--repo-root", str(case.root)]) == 0
    for _ in range(14):
        tick(case, capsys)
    assert_requests(case, 1, when="later ticks of the same stall", reason="quartz lantern")


def test_a_new_merge_ends_the_stall_and_the_next_stall_adds_one_new_request(stall_case, capsys):
    case = stall_case
    stalled_world(case)
    retire(case, "related-check group empty (quartz lantern)")
    for _ in range(12):  # Up to 10:05, 63 minutes since the 09:02 merge.
        tick(case, capsys)
    assert_requests(case, 1, when="first stall", reason="quartz lantern")

    # 10:08: member 741 merges; 863 is still ready. The stall is over.
    at(case, 3)
    land(case, MEMBERS[0])
    retire(case, "related-check group empty (amber kettle)")
    at(case, 3)  # 10:11: ticks now fall 8 .. 58 and 63 minutes after the new merge.
    for _ in range(11):
        tick(case, capsys)
        assert_none(case, MEMBERS[1:], before=1, when=f"{since_merge(case)} minutes after the new merge")

    tick(case, capsys)
    assert since_merge(case) == 63
    assert_requests(case, 2, MEMBERS[1:], when="first tick of the second stall", reason="amber kettle")
    for _ in range(6):
        tick(case, capsys)
    assert_requests(case, 2, MEMBERS[1:], when="later ticks of the second stall", reason="amber kettle")
