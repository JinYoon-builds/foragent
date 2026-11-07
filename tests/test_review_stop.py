"""processes.StopScope: a review stop signals verified recorded processes only (#2656)."""

import signal

import pytest

from foragent import processes


LEADER, HELPER, CHILD = 42000, 42001, 42002


@pytest.fixture
def scope(fake_process_table, tmp_path):
    table = fake_process_table
    for pid, parent, birth in ((LEADER, table.self_pid, 10), (HELPER, LEADER, 11), (CHILD, HELPER, 12)):
        table.add(pid, ppid=parent, pgid=LEADER, sid=LEADER, cwd=tmp_path, start_time=birth)
    stop = processes.StopScope()
    stop.record([{"pid": pid, "start_time": birth} for pid, birth in ((LEADER, 10), (HELPER, 11), (CHILD, 12))])
    return table, stop


def test_helpers_left_by_an_exited_leader_are_stopped_one_by_one(scope):
    table, stop = scope
    table.exit(LEADER)  # Its helpers reparent to pid 1 and keep running.

    stopped = stop.interrupt(signal.SIGTERM)

    assert stopped.signum == signal.SIGTERM and stopped.left == ()
    assert table.signals == [("pid", CHILD, signal.SIGTERM), ("pid", HELPER, signal.SIGTERM)]
    assert not {HELPER, CHILD} & table.live


def test_survivors_get_sigkill_and_a_second_signal_reuses_the_first_stop(scope):
    table, stop = scope
    table.ignore_term.update({LEADER, HELPER, CHILD})

    first = stop.interrupt(signal.SIGINT)
    again = stop.interrupt(signal.SIGTERM)

    kinds = {(target, number) for kind, target, number in table.signals}
    assert {(pid, signal.SIGKILL) for pid in (LEADER, HELPER, CHILD)} <= kinds
    assert all(kind == "pid" for kind, target, number in table.signals), "No group signal"
    assert again.signum == first.signum == signal.SIGINT and len(table.signals) == 6
    with pytest.raises(processes.ReviewStopped):
        stop.refuse_launch()


def test_a_missing_ancestor_in_the_middle_refuses_the_child(scope, monkeypatch):
    table, stop = scope
    listing = table.list_processes
    monkeypatch.setattr(processes, "list_processes",
                        lambda: tuple(row for row in listing() if row.pid != HELPER))

    stopped = stop.interrupt(signal.SIGTERM)

    assert (CHILD, f"ancestor {HELPER} is missing from the process table") in stopped.left
    assert ("pid", CHILD, signal.SIGTERM) not in table.signals and CHILD in table.live


def test_kill_process_group_also_refuses_a_child_of_an_unreadable_ancestor(scope, monkeypatch):
    table, _stop = scope
    listing = table.list_processes
    monkeypatch.setattr(processes, "list_processes", lambda: processes.ProcessTable(
        [row for row in listing() if row.pid != HELPER], unreadable=(HELPER,)))
    identities = [{"pid": pid, "start_time": birth} for pid, birth in ((LEADER, 10), (HELPER, 11), (CHILD, 12))]
    monkeypatch.setattr(processes, "observe_tree", lambda known, *args, **kwargs: list(known))
    monkeypatch.setattr(processes, "read_process_identity", lambda pid: next(
        (processes.ProcessIdentity(row.pid, row.ppid, row.pgid, row.sid, row.start_time)
         for row in listing() if row.pid == pid), None))

    with processes.recorded_tree(identities):
        processes.kill_process_group(LEADER)

    assert ("pid", CHILD, signal.SIGTERM) not in table.signals and CHILD in table.live
    assert ("pid", LEADER, signal.SIGTERM) in table.signals


@pytest.mark.parametrize("stage", [signal.SIGTERM, signal.SIGKILL])
def test_a_pid_reused_after_the_scan_is_never_signalled(scope, tmp_path, stage):
    table, stop = scope
    if stage == signal.SIGKILL:
        table.ignore_term.update({LEADER, HELPER, CHILD})

    def reuse_helper(call):
        if call == ("pid", CHILD, stage) and HELPER in table.live:
            table.dead.add(HELPER)
            table.add(HELPER, ppid=1, cwd=tmp_path, start_time=500)  # unrelated replacement

    table.after_signal = reuse_helper

    stopped = stop.interrupt(signal.SIGTERM)

    assert ("pid", HELPER, stage) not in table.signals and HELPER in table.live
    assert table.rows[HELPER].start_time == 500
    assert (HELPER, processes.REUSED) in stopped.left


def test_a_second_signal_during_the_stop_is_ignored_without_a_deadlock(scope):
    table, stop = scope
    nested = []

    def second_signal(call):
        if not nested:
            nested.append(stop.interrupt(signal.SIGINT))  # same thread, stop still running

    table.after_signal = second_signal
    result = []
    worker = __import__("threading").Thread(target=lambda: result.append(stop.interrupt(signal.SIGTERM)), daemon=True)
    worker.start()
    worker.join(5)

    assert not worker.is_alive(), "The reentrant handler must not wait on the stop's own lock"
    assert nested == [None] and result[0].signum == signal.SIGTERM
    assert not {LEADER, HELPER, CHILD} & table.live


def test_an_unrecorded_newer_child_of_a_member_is_stopped_too(scope, tmp_path):
    table, stop = scope
    late_child = 42003
    table.add(late_child, ppid=CHILD, pgid=LEADER, sid=LEADER, cwd=tmp_path, start_time=13)
    stranger = 42004  # older than its claimed parent: a reused-parent artefact, not a child
    table.add(stranger, ppid=CHILD, cwd=tmp_path, start_time=5)

    stop.interrupt(signal.SIGTERM)

    assert ("pid", late_child, signal.SIGTERM) in table.signals and late_child not in table.live
    assert all(target != stranger for kind, target, number in table.signals)


def test_a_launch_recorded_after_the_stop_is_stopped_on_arrival(fake_process_table, tmp_path):
    table = fake_process_table
    stop = processes.StopScope()
    assert stop.interrupt(signal.SIGINT).left == ()  # nothing recorded yet
    table.add(LEADER, ppid=table.self_pid, pgid=LEADER, sid=LEADER, cwd=tmp_path, start_time=10)
    table.add(HELPER, ppid=LEADER, pgid=LEADER, sid=LEADER, cwd=tmp_path, start_time=11)

    stop.record([{"pid": LEADER, "start_time": 10}])

    assert {("pid", HELPER, signal.SIGTERM), ("pid", LEADER, signal.SIGTERM)} <= set(table.signals)
    assert not {LEADER, HELPER} & table.live


def test_waits_end_once_the_review_stops_even_with_a_process_left_running(monkeypatch):
    def failed_scan():
        raise PermissionError("process scan unavailable")

    monkeypatch.setattr(processes, "list_processes", failed_scan)

    class Forever:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            raise processes.subprocess.TimeoutExpired("reviewer", timeout)

    sleeps = []
    with processes.stop_scope() as stop:
        stop.record([{"pid": LEADER, "start_time": 10}])
        stopped = stop.interrupt(signal.SIGTERM)
        assert stopped.left == ((None, "process scan unavailable"),)
        with pytest.raises(processes.ReviewStopped):
            processes._wait_with_deadline(Forever(), 3600)
        with pytest.raises(processes.ReviewStopped):
            processes.pause(3600, sleeps.append)
    assert sum(sleeps) < 1
    processes.pause(7, sleeps.append)  # outside a scope: one unchanged sleep
    assert sleeps[-1] == 7


# r1144 follow-up (#2672): membership found during a stop is kept, and only stable rows add it.
LATE, UNRELATED = 42003, 42004


def test_a_child_of_a_pid_reused_during_enumeration_is_never_signalled(scope, monkeypatch, tmp_path):
    """One listing can hold the helper's old row and a child of the PID's new owner (torn scan)."""
    table, stop = scope
    old_helper = table.rows[HELPER]
    table.exit(HELPER)  # CHILD reparents to 1; an unrelated process reuses the PID and spawns
    table.add(HELPER, ppid=1, cwd=tmp_path, start_time=25, argv=("unrelated",))
    table.add(LATE, ppid=HELPER, cwd=tmp_path, start_time=30, argv=("unrelated-child",))
    listing, calls = table.list_processes, []

    def torn():
        calls.append(1)
        rows = listing()
        if len(calls) % 2:  # every other listing read the helper's row before its PID was reused
            rows = tuple(old_helper if row.pid == HELPER else row for row in rows)
        return rows

    monkeypatch.setattr(processes, "list_processes", torn)
    table.ignore_term.update({LATE, HELPER})  # keep both alive so later stages scan them too

    stop.interrupt(signal.SIGTERM)

    assert all(target not in (LATE, HELPER) for kind, target, number in table.signals), table.signals
    assert {LATE, HELPER} <= table.live


@pytest.mark.parametrize("found", ["during-sigterm", "after-sigterm"])
def test_a_late_child_stays_a_member_after_its_helper_exits(scope, monkeypatch, tmp_path, found):
    """A child seen once is kept by PID and start time, though it reparents to pid 1 later."""
    table, stop = scope
    table.ignore_term.update({LATE, HELPER})
    listing, seen = table.list_processes, []

    if found == "during-sigterm":
        table.add(LATE, ppid=HELPER, pgid=LATE, sid=LEADER, cwd=tmp_path, start_time=30)
    else:
        def spawn(call):
            if call == ("pid", HELPER, signal.SIGTERM) and LATE not in table.rows:
                table.add(LATE, ppid=HELPER, pgid=LATE, sid=LEADER, cwd=tmp_path, start_time=30)

        table.after_signal = spawn

    descendants = processes._descendants

    def seen_then_helper_exits(*args):
        members = descendants(*args)
        if LATE in members:
            seen.append(1)  # the stop has seen the child under its verified helper
        return members

    def listing_after_seen():
        if seen and HELPER in table.live:
            table.exit(HELPER)  # LATE reparents to pid 1 and keeps running
        return listing()

    monkeypatch.setattr(processes, "_descendants", seen_then_helper_exits)
    monkeypatch.setattr(processes, "list_processes", listing_after_seen)

    stopped = stop.interrupt(signal.SIGTERM)

    assert HELPER not in table.live, "The helper exits during the stop"
    assert ("pid", LATE, signal.SIGKILL) in table.signals and LATE not in table.live, table.signals
    assert stopped.left == ()


def test_a_member_alive_after_sigkill_is_named(scope, monkeypatch):
    table, stop = scope
    table.ignore_term.update({LEADER, HELPER, CHILD})
    real = table._signal

    def unkillable(kind, target, number, members):
        if number == signal.SIGKILL and target == CHILD:
            table.calls.append((kind, target, number))
            table.signals.append((kind, target, number))
            return  # accepted, but the process stays (uninterruptible, say)
        return real(kind, target, number, members)

    table._signal = unkillable

    stopped = stop.interrupt(signal.SIGTERM)

    assert (CHILD, processes.STILL_RUNNING) in stopped.left and CHILD in table.live


class Launched:
    pid = LEADER

    def __init__(self, argv, **kwargs):
        self.stdin = __import__("io").BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        __import__("time").sleep(min(timeout or 0.05, 0.05))
        if self.returncode is None:
            raise processes.subprocess.TimeoutExpired("reviewer", timeout)
        return self.returncode


@pytest.mark.parametrize("finished", [False, True])
def test_a_launch_whose_identity_is_unreadable_is_named_by_the_stop(
        fake_process_table, monkeypatch, tmp_path, finished):
    def unreadable(*args, **kwargs):
        raise PermissionError("process rows unreadable")

    launched = []

    def popen(argv, **kwargs):
        launched.append(Launched(argv, **kwargs))
        return launched[-1]

    monkeypatch.setattr(processes, "read_process_identity", unreadable)
    monkeypatch.setattr(processes, "observe_tree", unreadable)
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    threading = __import__("threading")
    errors = []

    with processes.stop_scope() as stop:
        def reviewer():
            try:
                processes.run_logged(["fake-reviewer"], cwd=tmp_path, timeout_s=30,
                                     log_path=tmp_path / "reviewer.log", prompt="review")
            except BaseException as exc:
                errors.append(exc)

        # Reviewers run in a copy of the context, as _panel_review does, so the scope reaches them.
        worker = threading.Thread(target=__import__("contextvars").copy_context().run, args=(reviewer,))
        worker.start()
        while not launched:
            __import__("time").sleep(0.01)
        stopped = stop.interrupt(signal.SIGTERM)
        stop.settle(5)
        if finished:
            launched[0].returncode = 0
        report = stop.report()
    worker.join(5)

    assert not worker.is_alive() and isinstance(errors[0], processes.ReviewStopped), "".join(__import__("traceback").format_exception(errors[0]))
    assert stopped.left == ()
    assert report == ([] if finished else [(LEADER, processes.LAUNCH_UNREAD)])


@pytest.mark.parametrize("finishes", [True, False])
def test_the_report_waits_for_launches_in_flight_or_names_them(finishes):
    threading, time = __import__("threading"), __import__("time")
    stop = processes.StopScope()
    release = threading.Event()

    def reviewer():
        with stop.launching():
            release.wait(5)
            stop.left.append((LEADER, "recorded by a late stop"))

    worker = threading.Thread(target=reviewer)
    worker.start()
    stop.interrupt(signal.SIGTERM)
    if finishes:
        threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    stop.settle(1.0)
    waited = time.monotonic() - started
    report = stop.report()
    release.set()
    worker.join(5)

    if finishes:
        assert waited < 1.0 and report == [(LEADER, "recorded by a late stop")]
    else:
        assert waited >= 1.0 and report == [(None, "1 reviewer launch(es) were still being registered")]


@pytest.mark.parametrize("member", ["late-child", "recorded"])
def test_a_member_whose_row_turns_unreadable_is_named_not_assumed_gone(scope, monkeypatch, tmp_path, member):
    """After SIGTERM a member's row becomes unreadable: that is no proof it ended."""
    table, stop = scope
    target = LATE if member == "late-child" else CHILD
    table.ignore_term.update({target, HELPER})  # the helper outlives SIGTERM until LATE is verified
    if member == "late-child":
        def spawn(call):
            if call == ("pid", HELPER, signal.SIGTERM) and LATE not in table.rows:
                table.add(LATE, ppid=HELPER, pgid=LATE, sid=LEADER, cwd=tmp_path, start_time=30)

        table.after_signal = spawn
    listing, seen = table.list_processes, []
    descendants = processes._descendants

    def seen_members(*args):
        members = descendants(*args)
        if target in members and target in table.live:
            seen.append(1)
        return members

    def hiding():
        if seen and HELPER in table.live:
            table.exit(HELPER)  # the helper exits once the stop has verified its member
        rows = listing()
        if seen:  # from the next listing on, the member's row cannot be read
            return processes.ProcessTable([row for row in rows if row.pid != target], unreadable=(target,))
        return rows

    monkeypatch.setattr(processes, "_descendants", seen_members)
    monkeypatch.setattr(processes, "list_processes", hiding)

    stopped = stop.interrupt(signal.SIGTERM)

    assert target in table.live
    assert (target, "its process row is unreadable") in stopped.left, (stopped.left, table.signals)
    assert (target, "its process row is unreadable") in stop.report()
