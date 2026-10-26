"""Acceptance test for overlap-lock-load. Locked by Foragent; do not edit."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from foragent import overlap_files, run_overlap
from foragent.issue import body_hash, parse_body
from test_optimistic_overlap import (
    CLI,
    _change,
    _finish_peer,
    _issue,
    _peer,
    _runner_case,
    isolated_home,  # noqa: F401
    offline_acceptance,  # noqa: F401
    offline_overlap,  # noqa: F401
)
from test_process_snapshot_per_tick import (
    fake_processes,  # noqa: F401
    snapshot_io,  # noqa: F401
)


def _approved(case, number, files, *, running=False):
    _issue(case.ctx, number, files, running=running)
    body = case.tracker.get_issue(number).body
    case.store.save_approval(
        number, body=body, body_sha=body_hash(body), acceptance_blobs={},
        approved_by="human", approved_at=case.clock[0].isoformat(),
    )


def _running_case(git_history, tmp_path, monkeypatch, table, *, count=1):
    case = _runner_case(git_history, tmp_path, monkeypatch, [CLI], overlap_per_file=1)
    for number in range(1, count + 1):
        worktree = _change(case.ctx, number, {}, commit=False)
        root = table.add(7000 + number, cwd=worktree, start_time=case.clock[0].timestamp())
        if number == 1:
            case.store.update_run(
                number, pid=root.pid,
                process_identities=[{"pid": root.pid, "start_time": root.start_time}],
            )
        else:
            _approved(case, number, [f"src/foragent/peer_{number}.py"], running=True)
        assert case.store.get_run(number).process_identities == [
            {"pid": root.pid, "start_time": root.start_time},
        ], "Nonempty fake ownership must exercise the expensive observation path"
    return case


def _measure(case, number=1):
    parsed = parse_body(case.store.load_approval(number).body)
    return overlap_files.measure(case.ctx, case.store.get_run(number), parsed, "main")


def _watch_store(store, table, monkeypatch):
    """Count real persistence and lock entries without replacing their behavior."""
    calls = SimpleNamespace(writes=[], locks=0)
    write_run, state_lock = store._write_run, store._state_lock

    def write(run):
        calls.writes.append(run)
        return write_run(run)

    @contextmanager
    def lock():
        calls.locks += 1
        with state_lock():
            yield

    monkeypatch.setattr(store, "_write_run", write)
    monkeypatch.setattr(store, "_state_lock", lock)
    table.list_calls = 0
    return calls


def _counts(calls, table):
    return {"run_writes": len(calls.writes), "state_locks": calls.locks,
            "process_listings": table.list_calls}


def test_overlap_lock_load_unchanged_measure_and_start_checks_are_read_only(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = _running_case(git_history, tmp_path, monkeypatch, fake_processes, count=4)
    _approved(case, 5, [CLI])
    _approved(case, 6, ["src/foragent/unclaimed.py"])
    measured = {number: _measure(case, number) for number in range(1, 5)}
    assert all(run.changed_files == [] and run.files_base_branch == "main"
               and run.effective_files == run.declared_files for run in measured.values())
    calls = _watch_store(case.store, fake_processes, monkeypatch)

    second = _measure(case)
    second_load = _counts(calls, fake_processes)
    calls.writes.clear()
    calls.locks = fake_processes.list_calls = 0
    # Exercise the admission check used by a tick, excluding unrelated tick writes.
    blocked = overlap_files.can_start(case.ctx, "orch", 5)
    free = overlap_files.can_start(case.ctx, "orch", 6)
    start_load = _counts(calls, fake_processes)

    assert second == measured[1] == case.store.get_run(1)
    assert blocked is False and free is True, "File overlap admission decisions must stay unchanged"
    assert not fake_processes.signals
    zero = {"run_writes": 0, "state_locks": 0, "process_listings": 0}
    assert {"second_measure": second_load, "four_run_start_checks": start_load} == {
        "second_measure": zero, "four_run_start_checks": zero,
    }, "Unchanged overlap checks must neither write run records, take the state lock nor list processes"


def test_overlap_lock_load_metadata_update_skips_listing_but_process_update_observes(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = _running_case(git_history, tmp_path, monkeypatch, fake_processes)
    before = case.store.get_run(1)
    child = fake_processes.add(
        7500, ppid=before.pid, cwd=case.store.worktree_path(1),
        start_time=case.clock[0].timestamp() + 1,
    )
    stamp = (case.clock[0] + timedelta(seconds=30)).isoformat()
    fake_processes.list_calls = 0

    metadata = case.store.update_run(1, last_progress_at=stamp)
    metadata_listings = fake_processes.list_calls
    fake_processes.list_calls = 0
    observed = case.store.update_run(1, process_identities=before.process_identities)
    ownership_listings = fake_processes.list_calls

    assert metadata.last_progress_at == case.store.get_run(1).last_progress_at == stamp
    assert ownership_listings > 0, "Explicit process updates must still inspect the fake process table"
    assert {"pid": child.pid, "start_time": child.start_time} in observed.process_identities
    assert not fake_processes.signals
    assert metadata_listings == 0, (
        f"A metadata-only update_run scanned the process table {metadata_listings} times under the state lock"
    )
    assert metadata.process_identities == before.process_identities


def test_overlap_lock_load_late_file_wait_polls_no_faster_than_fifteen_seconds(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = _running_case(git_history, tmp_path, monkeypatch, fake_processes)
    _peer(case, ["feature.txt"], exclusive=["feature.txt"])
    _change(case.ctx, 1, {"feature.txt": "Late addition\n"}, commit=False)
    parsed = parse_body(case.store.load_approval(1).body)
    previous_phase = case.store.get_run(1).phase
    sleeps = []

    def sleep(seconds):
        assert len(sleeps) < 3, "Releasing the actual holder must end the wait"
        assert case.store.get_run(2).status == "running"
        assert case.store.get_run(1).waiting_until is not None
        sleeps.append(seconds)
        case.clock[0] += timedelta(seconds=seconds)
        if len(sleeps) == 3:
            _finish_peer(case)

    case.ctx.sleep = sleep
    run_overlap.wait_for_overlap(SimpleNamespace(
        ctx=case.ctx, n=1, parsed=parsed, target_branch="main",
    ))

    saved = case.store.get_run(1)
    assert saved.late_files == ["feature.txt"], "Exercise a real undeclared worktree addition"
    assert case.store.get_run(2).status == "finished"
    assert saved.waiting_until is None and saved.phase == previous_phase
    assert saved.waited_seconds == sum(sleeps), "Overlap waiting must remain credited to the run"
    assert not fake_processes.signals
    assert len(sleeps) == 3 and all(seconds >= 15 for seconds in sleeps), (
        f"Blocked late-file checks must sleep at least 15 seconds between polls; got {sleeps}"
    )


def test_overlap_lock_load_new_file_is_written_once_then_measurement_settles(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = _running_case(git_history, tmp_path, monkeypatch, fake_processes)
    assert _measure(case).changed_files == []
    calls = _watch_store(case.store, fake_processes, monkeypatch)
    new_file = "src/foragent/new_file.py"
    worktree = _change(case.ctx, 1, {new_file: "VALUE = 1\n"}, commit=False)
    assert (worktree / new_file).read_text() == "VALUE = 1\n"

    changed = _measure(case)
    settled = _measure(case)

    assert changed == settled == case.store.get_run(1)
    assert changed.changed_files == changed.late_files == [new_file]
    assert changed.declared_files == changed.effective_files == [CLI]
    assert changed.files_committed is False
    assert calls.writes and calls.writes[0].changed_files == [new_file]
    assert not fake_processes.signals
    assert _counts(calls, fake_processes) == {
        "run_writes": 1, "state_locks": 1, "process_listings": 0,
    }, "A new file needs one metadata write; measuring that same file set again must be read-only"
