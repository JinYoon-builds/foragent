"""Acceptance tests for promises-shelf: promises are a shelf place the gate checks every tick.

Any session records "when X happens, do Y, owner Z" with one fa command and returns. The gate
moves each promise: waiting, then due when X happens (the owner is told and the owner's next run
prompt lists it), then late after its limit (one alarm one level up, then the /fa report's second
line), then done (removed).

Public contract these tests read (choices made for this issue):

- ``fa orch promise add --when <event> --do <action> --owner <owner> [--limit-minutes N]``
  records a promise, prints its id and returns at once. Events: ``closed:<N>``, ``merged:<N>``
  (issue or PR number), ``approved:<N>`` and ``time:<ISO 8601 time with a timezone>``.
  An event that cannot be checked (unknown issue, malformed time, free text) is refused with a
  failing exit and nothing is recorded.
- ``fa orch promise list --json`` lists the open promises: ``[{"id", "state", ...}]`` with
  ``state`` one of ``waiting``, ``due``, ``late``. ``fa orch promise done <id>`` removes one.
- Owners are ``pm``, ``orchestrator``, a lane name or a role name. When a promise becomes due the
  gate adds one orchestrator request box record ``kind: promise-due``, ``promise: <id>``,
  ``to: <owner>``. When it becomes late it adds one record ``kind: promise-late``,
  ``promise: <id>``, ``to: <one level up>`` (the orchestrator's level up is the PM).
- Promises are a place on the shelf object (#2332): ``foragent.shelf.places()`` holds a place
  named ``promises`` and ``fa brief --json`` shows its row under ``shelves``.
- The /fa report (``fa brief --report``) and the dashboard show "promises N, late M".

Every check goes through public entry points: the real ``fa`` CLI with an offline FakeTracker,
real ``fa gate tick`` runs (tick_case), the runner (``run_issue``) for the lane's run prompt and
the dashboard frame builder. No process is started and no signal is sent.
"""

from __future__ import annotations

from datetime import timedelta
import json
import re
import time

import pytest

from shelf_object_support import brief, report_lines, requests, shelf_module, shelf_row, tick_at
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)


JUDGMENT = "Judgment: keep the storage order waiting for the schema decision."
GOAL_BODY = ("<!-- foragent:v1 -->\n## Goal\nDo the work.\n## Out of scope\nOther work.\n"
             "## Done when\n```sh\ntrue\n```\n")
COUNTS = re.compile(r"promises?\D{0,4}?(\d+)\D{0,6}?late\D{0,4}?(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------- helpers


def run(case, capsys, *argv):
    capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    output = capsys.readouterr()
    return code, output.out, output.err


def listed(case, capsys):
    code, out, err = run(case, capsys, "orch", "promise", "list", "--json")
    assert code == 0, f"fa orch promise list --json failed ({code}): {err}"
    rows = json.loads(out)
    assert isinstance(rows, list), rows
    return {row["id"]: row for row in rows}


def promise(case, capsys, when, do, owner, *, limit=None):
    """Record one promise through the public command and return the id it printed."""
    before = set(listed_or_empty(case, capsys))
    argv = ["orch", "promise", "add", "--when", when, "--do", do, "--owner", owner]
    if limit is not None:
        argv += ["--limit-minutes", str(limit)]
    code, out, err = run(case, capsys, *argv)
    assert code == 0, f"fa orch promise add --when {when} must record the promise ({code}): {err}{out}"
    new = set(listed(case, capsys)) - before
    assert len(new) == 1, f"One command records exactly one promise: {new}"
    (number,) = new
    assert str(number) in out, f"The command prints the new promise's id {number!r}: {out!r}"
    return number


def listed_or_empty(case, capsys):
    code, out, _err = run(case, capsys, "orch", "promise", "list", "--json")
    return {row["id"]: row for row in json.loads(out)} if code == 0 else {}


def state(case, capsys, number):
    row = listed(case, capsys).get(number)
    return None if row is None else row.get("state")


def records(case, capsys, kind, number, to=None):
    return [row for row in requests(case, capsys)
            if row.get("kind") == kind and str(row.get("promise")) == str(number)
            and (to is None or row.get("to") == to)]


def tick(case, capsys, minutes=5):
    tick_at(case, capsys, case.clock[0] + timedelta(minutes=minutes))


def open_issue(case, number, labels=()):
    case.tracker.add_issue(number, f"Work {number}", GOAL_BODY, labels=list(labels))


def counts(text):
    found = COUNTS.findall(text)
    return [(int(open_count), int(late)) for open_count, late in found]


def dashboard(case):
    from foragent import status, watch
    from foragent.config import load_config
    from test_squirrel_running import plain_frame

    config = load_config(case.root)
    now = case.clock[0]
    payload = status.collect(case.store, config, now)
    snapshot = watch.collect(payload, case.tracker, case.store, config, now)
    return plain_frame(watch.render_frame(snapshot, config, case.root, width=180, now=now))


def names(line, number, action):
    """How often a report line names a promise: its action text, or its id as a whole token when the id
    is distinctive (a bare one- or two-digit id would match any count on the line)."""
    ident = str(number)
    found = line.count(action)
    if len(ident) >= 3 or not ident.isdigit():
        found += len(re.findall(rf"(?<![\w-]){re.escape(ident)}(?![\w-])", line))
    return found


def judge(case):
    from foragent.orch_events import append_event

    append_event(case.store, case.clock[0], None, "note", "Agent's current judgment", say=JUDGMENT)


# ---------------------------------------------------------------- statement 1: one command, returns at once


def test_one_command_records_a_promise_prints_its_id_and_returns_at_once(tick_case, capsys, monkeypatch):
    case = tick_case
    open_issue(case, 1952)
    tick(case, capsys)

    def no_wait(*_args, **_kwargs):
        raise AssertionError("Recording a promise must not wait for its event")

    started = case.clock[0]
    far = (started + timedelta(days=3)).isoformat()
    # Scope the sleep guard to the recording commands only, so tick_case's own patches stay installed.
    with monkeypatch.context() as scoped:
        scoped.setattr(time, "sleep", no_wait)
        first = promise(case, capsys, "closed:1952", "publish order sky after #1952", "orchestrator")
        second = promise(case, capsys, f"time:{far}", "rerun prepare for order sky", "pm")
    assert case.clock[0] == started, "The command returns without waiting for any tick"
    assert first != second, "Each promise gets its own id"
    shown = listed(case, capsys)
    assert {shown[first]["state"], shown[second]["state"]} == {"waiting"}, shown
    assert len(shown) == 2, f"Two commands record two promises, not more: {shown}"

    tick(case, capsys)
    assert len(listed(case, capsys)) == 2, "A tick before the events keeps both promises waiting, not doubled"
    assert state(case, capsys, first) == "waiting" and state(case, capsys, second) == "waiting"


# ---------------------------------------------------------------- statement 5: unchecked events are refused


@pytest.mark.parametrize("when, named", [
    ("closed:98765", "98765"),
    ("merged:98765", "98765"),
    ("approved:98765", "98765"),
    ("time:tomorrow-afternoon", "tomorrow-afternoon"),
    ("time:2026-13-45T25:61:00+00:00", "2026-13-45"),
    ("when the CI looks good", "CI looks good"),
])
def test_a_promise_whose_event_cannot_be_checked_is_refused_when_recorded(tick_case, capsys, when, named):
    case = tick_case
    open_issue(case, 1952)
    tick(case, capsys)
    kept = promise(case, capsys, "closed:1952", "publish order sky after #1952", "orchestrator")

    code, out, err = run(case, capsys, "orch", "promise", "add", "--when", when,
                         "--do", "publish the next order", "--owner", "orchestrator")
    assert code != 0, f"An event the gate cannot check ({when!r}) is refused, not kept waiting: {out}"
    assert named in out + err, f"The refusal names what cannot be checked ({named!r}): {out}{err}"
    assert set(listed(case, capsys)) == {kept}, "A refused promise is not recorded"
    for _ in range(2):
        tick(case, capsys)
    assert set(listed(case, capsys)) == {kept}, "Nothing refused shows up later as waiting"


# ---------------------------------------------------------------- statement 2: due within one tick, owner told


def happen(case, kind, number):
    """Make the event happen the way the gate and GitHub record it."""
    if kind == "closed":
        case.tracker.close_issue(number)
    elif kind == "merged":
        case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
        case.tracker.close_issue(number)
        case.store.record_merge(number, case.clock[0])
        case.store.finish_run(number, outcome="merged")
    elif kind == "approved":
        from foragent.issue import body_hash

        # A person approves: the label (FakeTracker refuses it from gate code) and the approval record.
        case.tracker.add_issue(number, f"Work {number}", GOAL_BODY, labels=["approved"])
        case.store.save_approval(number, body=GOAL_BODY, body_sha=body_hash(GOAL_BODY), acceptance_blobs={},
                                 approved_by="human", approved_at=case.clock[0].isoformat())
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["closed", "merged", "approved", "time"])
def test_the_gate_marks_a_promise_due_within_one_tick_and_tells_the_owner_once(tick_case, capsys, kind):
    case = tick_case
    open_issue(case, 2191)
    open_issue(case, 2192)
    tick(case, capsys)
    when = f"time:{(case.clock[0] + timedelta(minutes=12)).isoformat()}" if kind == "time" else f"{kind}:2191"
    number = promise(case, capsys, when, "rerun prepare after the event", "orchestrator")
    other = promise(case, capsys, f"{'closed' if kind == 'time' else kind}:2192", "publish order moon", "orchestrator")

    tick(case, capsys)
    tick(case, capsys)
    assert state(case, capsys, number) == "waiting", "Before its event a promise waits"
    assert records(case, capsys, "promise-due", number) == [], "Nobody is told before the event"

    if kind != "time":
        happen(case, kind, 2191)
    tick(case, capsys)  # time: the clock is now 15 minutes past recording, 3 past the event.
    assert state(case, capsys, number) == "due", "The first tick after the event marks the promise due"
    told = records(case, capsys, "promise-due", number)
    assert len(told) == 1, f"The owner is told once that the promise is due: {told}"
    assert told[0]["to"] == "orchestrator", told[0]
    assert told[0].get("status") != "done", "The notice is open work for the owner"
    assert state(case, capsys, other) == "waiting", "A promise whose event did not happen stays waiting"
    assert records(case, capsys, "promise-due", other) == []

    for _ in range(2):
        tick(case, capsys)
    assert len(records(case, capsys, "promise-due", number)) == 1, "Later ticks do not tell the owner again"
    assert state(case, capsys, number) == "due"


def test_the_owner_lanes_next_run_prompt_lists_its_due_promises_only(tick_case, capsys):
    from foragent.runner import run_issue

    case = tick_case
    open_issue(case, 2201)
    open_issue(case, 2202)
    tick(case, capsys)
    due = promise(case, capsys, "closed:2201", "rebase the storage branch after #2201", "main")
    waiting = promise(case, capsys, "closed:2202", "drop the shim after #2202", "main")
    elsewhere = promise(case, capsys, "closed:2201", "publish order lantern after #2201", "orchestrator")
    case.tracker.close_issue(2201)
    tick(case, capsys)
    assert state(case, capsys, due) == "due" and state(case, capsys, waiting) == "waiting"

    # The lane "main" runs its next issue: issue 1 comes back approved and starts a run.
    issue = case.tracker.get_issue(1)
    case.tracker.add_issue(1, issue.title, issue.body, labels=["approved", "running"])
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    # The runner gets the offline executors of make_case (the tick bound ctx.config to the YAML config).
    case.ctx.store, case.ctx.config = case.store, case.config
    run_issue(1, case.ctx)
    assert case.implementer.calls, "The lane's run started an implementation session"
    prompt = case.implementer.calls[0]["prompt"]
    assert "rebase the storage branch after #2201" in prompt, "The owner's next run prompt lists its due promise"
    assert str(due) in prompt, "The listed promise carries its id, so the owner can mark it done"
    assert "drop the shim after #2202" not in prompt, "A promise still waiting is not listed"
    assert "publish order lantern" not in prompt, "Another owner's promise is not listed"
    assert state(case, capsys, elsewhere) == "due", "The other owner's promise is due too, yet not this lane's"


# ---------------------------------------------------------------- statement 3: late, one alarm up, report line 2


def test_a_due_promise_not_done_within_its_limit_becomes_late_alarms_one_level_up_once(tick_case, capsys):
    case = tick_case
    for number in (2301, 2302, 2303):
        open_issue(case, number)
    tick(case, capsys)
    late = promise(case, capsys, "closed:2301", "publish order sky after #2301", "orchestrator", limit=30)
    kept = promise(case, capsys, "closed:2302", "publish order moon after #2302", "orchestrator", limit=30)
    never = promise(case, capsys, "closed:2303", "publish order sun after #2303", "orchestrator", limit=30)

    for _ in range(8):  # 40 minutes: a promise still waiting is never late, whatever its age.
        tick(case, capsys)
    assert state(case, capsys, never) == "waiting"

    case.tracker.close_issue(2301)
    case.tracker.close_issue(2302)
    tick(case, capsys)
    due_at = case.clock[0]
    assert state(case, capsys, late) == "due" and state(case, capsys, kept) == "due"
    code, _out, err = run(case, capsys, "orch", "promise", "done", str(kept))
    assert code == 0, err

    while case.clock[0] < due_at + timedelta(minutes=25):
        tick(case, capsys)
    assert state(case, capsys, late) == "due", "Within its limit a due promise is not late"
    assert records(case, capsys, "promise-late", late) == [], "No alarm within the limit"
    judge(case)
    assert not any("sky" in line for line in report_lines(case, capsys)[1:2]), "Not in the report before late"

    tick(case, capsys, minutes=10)  # 35 minutes after it became due.
    assert state(case, capsys, late) == "late", "Past its limit a due promise becomes late"
    alarms = records(case, capsys, "promise-late", late)
    assert len(alarms) == 1, f"One alarm goes one level up: {alarms}"
    assert alarms[0]["to"] == "pm", f"The orchestrator's level up is the PM: {alarms[0]}"
    assert records(case, capsys, "promise-late", late, to="orchestrator") == [], "The alarm is not sent to the owner"
    assert records(case, capsys, "promise-late", kept) == [], "A promise done in time is never late"
    assert records(case, capsys, "promise-late", never) == [], "A waiting promise is never late"

    judge(case)
    lines = report_lines(case, capsys)
    assert len(lines) >= 2, lines
    second = lines[1]
    assert names(second, late, "publish order sky") >= 1, (
        f"The /fa report names the late promise on its second line: {lines}")
    assert "moon" not in second and "sun after" not in second, f"Only the late promise is named: {second!r}"

    for _ in range(3):
        tick(case, capsys)
    assert len(records(case, capsys, "promise-late", late)) == 1, "Later ticks do not raise a second alarm"
    judge(case)
    again = report_lines(case, capsys)[1]
    assert names(again, late, "publish order sky") == names(second, late, "publish order sky"), (
        f"Another tick while late leaves the report naming it once, not doubled: {again!r}")


# ---------------------------------------------------------------- statement 4: done removes, counts shown


def test_done_removes_a_promise_and_the_report_and_dashboard_show_open_and_late_counts(tick_case, capsys):
    case = tick_case
    for number in (2401, 2402, 2403):
        open_issue(case, number)
    tick(case, capsys)
    late = promise(case, capsys, "closed:2401", "publish order sky after #2401", "orchestrator", limit=30)
    due = promise(case, capsys, "closed:2402", "publish order moon after #2402", "orchestrator", limit=600)
    waiting = promise(case, capsys, "closed:2403", "publish order sun after #2403", "orchestrator", limit=30)
    case.tracker.close_issue(2401)
    case.tracker.close_issue(2402)
    tick(case, capsys)
    tick(case, capsys, minutes=40)
    assert [state(case, capsys, number) for number in (late, due, waiting)] == ["late", "due", "waiting"]

    assert (3, 1) in counts("\n".join(report_lines(case, capsys))), (
        f"The /fa report shows promises 3, late 1: {report_lines(case, capsys)}")
    assert (3, 1) in counts(dashboard(case)), f"The dashboard shows promises 3, late 1: {dashboard(case)}"

    code, _out, err = run(case, capsys, "orch", "promise", "done", str(late))
    assert code == 0, f"fa orch promise done marks a promise done: {err}"
    shown = listed(case, capsys)
    assert late not in shown and set(shown) == {due, waiting}, f"A done promise is removed: {shown}"
    tick(case, capsys)
    assert set(listed(case, capsys)) == {due, waiting}, "A done promise does not come back on the next tick"
    report = "\n".join(report_lines(case, capsys))
    assert (2, 0) in counts(report), f"The /fa report shows promises 2, late 0: {report}"
    assert (3, 1) not in counts(report)
    assert (2, 0) in counts(dashboard(case)), f"The dashboard shows promises 2, late 0: {dashboard(case)}"
    assert len(records(case, capsys, "promise-late", late)) == 1, "Marking done raises no new alarm"

    code, _out, _err = run(case, capsys, "orch", "promise", "done", str(late))
    assert set(listed(case, capsys)) == {due, waiting}, "Marking a removed promise done again changes nothing"
    for number in (due, waiting):
        assert run(case, capsys, "orch", "promise", "done", str(number))[0] == 0
    assert listed(case, capsys) == {}


# ---------------------------------------------------------------- goal: promises are a shelf place


def test_promises_are_a_place_on_the_shelf_object_that_counts_open_promises(tick_case, capsys):
    """The shelf object (#2332) is this issue's prerequisite; its checks live here, after the promise
    commands, so the statement tests above stay red only on missing promise behaviour."""
    case = tick_case
    for number in (2501, 2502):
        open_issue(case, number)
    tick(case, capsys)
    first = promise(case, capsys, "closed:2501", "publish order kite after #2501", "orchestrator")
    second = promise(case, capsys, "closed:2502", "publish order reed after #2502", "orchestrator")
    case.tracker.close_issue(2501)
    tick(case, capsys)
    assert state(case, capsys, first) == "due" and state(case, capsys, second) == "waiting"

    assert "promises" in {place.name for place in shelf_module().places()}, (
        "Promises are a place on the shelf object")
    assert shelf_row(brief(case, capsys), "promises")["count"] == 2, "The shelf place counts open promises"
    assert run(case, capsys, "orch", "promise", "done", str(first))[0] == 0
    tick(case, capsys)
    assert shelf_row(brief(case, capsys), "promises")["count"] == 1, "A done promise leaves the shelf place"
    assert run(case, capsys, "orch", "promise", "done", str(second))[0] == 0
    assert shelf_row(brief(case, capsys), "promises")["count"] == 0
