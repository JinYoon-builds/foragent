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
