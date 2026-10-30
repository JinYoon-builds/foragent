"""Boundaries of capacity.hold_workload (#991): liveness, run identity, limits, test slots."""

from datetime import UTC, datetime, timedelta
import fcntl
from types import SimpleNamespace

import pytest

from foragent import capacity, capacity_sessions
from foragent.state import StateStore


@pytest.fixture
def case(tmp_path, fake_process_table, monkeypatch):
    store = StateStore(tmp_path / "machine" / "owner__project")
    monkeypatch.setattr(capacity, "machine_root", lambda: store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    capacity.write_state(store, 1, datetime.now(UTC))
    config = SimpleNamespace(language="en", test_slots=1)
    yield SimpleNamespace(store=store, config=config, table=fake_process_table, root=store.root.parent)
    assert fake_process_table.signals == [], "Workload accounting must never signal a process"


def now():
    return datetime.now(UTC)


def test_dead_workload_stops_counting_and_readers_keep_its_file(case):
    sibling = StateStore(case.root / "other")
    case.table.add(42001, cwd=case.root, start_time=1)
    path = capacity_sessions.add(sibling, pid=42001, kind="preread")
    assert capacity.machine_holders(case.store, now()) == ["other preread"]
    case.table.exit(42001)
    assert capacity.machine_runs(case.store, now()) == 0
    assert path.exists(), "Readers never remove another process's record"
    with capacity.hold_workload(case.store, case.config, wait_s=0):
        assert capacity.machine_runs(case.store, now()) == 1


def test_run_absorbs_its_own_process_once_and_extra_agents_count(case):
    pid = capacity.os.getpid()
    case.store.start_run(7, branch="b", worktree="w", pid=pid)
    capacity.write_state(case.store, 3, now())
    with capacity.hold_workload(case.store, case.config, kind="worker", issue=7):
        assert capacity.machine_runs(case.store, now()) == 1, "The run's own agent is not charged twice"
        # Two concurrent reviewers in other contexts: each is one more slot.
        import contextvars

        reviewers = []
        for _ in range(2):
            context, hold = contextvars.Context(), capacity.hold_workload(case.store, case.config, kind="reviewer")
            context.run(hold.__enter__)
            reviewers.append((context, hold))
        assert capacity.machine_runs(case.store, now()) == 3
        for context, hold in reviewers:
            context.run(hold.__exit__, None, None, None)
        assert capacity.machine_runs(case.store, now()) == 1


def test_timeout_names_holders_and_leaves_no_record(case, monkeypatch, capsys):
    sibling = StateStore(case.root / "other")
    sibling.start_run(99, branch="b", worktree="w", pid=capacity.os.getpid())
    clock = [0.0]
    monkeypatch.setattr(capacity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(capacity.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(TimeoutError, match="capacity"):
        with capacity.hold_workload(case.store, case.config, wait_s=5):
            pytest.fail("A full machine must not admit the workload")
    assert clock[0] == 5
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2 and "Ctrl-C" in lines[0] and "other #99" in lines[1] and "Retry" in lines[1]
    assert capacity_sessions.read(case.store.root) == []


def test_tick_try_defers_silently(case, monkeypatch, capsys):
    capacity.write_state(case.store, 1, now())
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "critical")
    with pytest.raises(TimeoutError, match="memory"):
        with capacity.hold_workload(case.store, case.config, wait_s=0):
            pytest.fail("Critical memory holds new work")
    assert capsys.readouterr().err == ""


def test_machine_ceiling_caps_the_aimd_value(case):
    capacity.write_state(case.store, 4, now())
    (case.root / "machine.yml").write_text("max_parallel: 1\n", encoding="utf-8")
    assert capacity.workload_budget(case.store) == 1


def test_tests_hold_one_test_slot_and_nesting_reuses_both(case, monkeypatch):
    taken = []
    original = capacity._take_test_slot

    def take(limit, deadline=None):
        stream = original(limit, deadline)
        taken.append(stream is not None)
        return stream

    monkeypatch.setattr(capacity, "_take_test_slot", take)
    with capacity.hold_workload(case.store, case.config, tests=True):
        with capacity.hold_workload(case.store, case.config, tests=True):
            assert capacity.machine_runs(case.store, now()) == 1
        assert taken == [True]
        # The one slot is held now: another holder cannot take it.
        assert capacity._take_test_slot(1) is None
    assert capacity.machine_runs(case.store, now()) == 0
    stream = capacity._take_test_slot(1)
    assert stream is not None, "Leaving the workload releases its test slot"
    stream.close()


def test_test_slot_wait_is_bounded(case, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(capacity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(capacity.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 1))
    held = capacity._take_test_slot(1)
    try:
        with pytest.raises(TimeoutError):
            with capacity.hold_workload(case.store, case.config, tests=True, wait_s=3):
                pytest.fail("No test slot was free")
    finally:
        held.close()
    assert capacity.machine_runs(case.store, now()) == 0, "A failed test-slot wait releases the machine slot"


def fake_clock(monkeypatch, step=None):
    clock = [0.0]
    monkeypatch.setattr(capacity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(capacity.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + (seconds if step is None else step)))
    return clock


# Defect class: a slot already reserved for this agent is reused only when counting.
def test_run_in_the_last_slot_reuses_it_for_its_own_agent(case, monkeypatch):
    case.store.start_run(7, branch="b", worktree="w", pid=capacity.os.getpid())
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", capacity.OVERLOAD_LOAD))
    import contextvars

    with capacity.hold_workload(case.store, case.config, kind="worker", issue=7, wait_s=0):
        assert capacity.machine_runs(case.store, now()) == 1
        reviewer = capacity.hold_workload(case.store, case.config, kind="reviewer", wait_s=0)
        with pytest.raises(TimeoutError):
            contextvars.Context().run(reviewer.__enter__)


def test_gate_reservation_is_reused_by_the_issue_it_reserved(case):
    case.store.prepare_run_start(8)
    with capacity.hold_workload(case.store, case.config, kind="worker", issue=8, wait_s=0):
        assert capacity.machine_runs(case.store, now()) == 1
    with pytest.raises(TimeoutError):
        with capacity.hold_workload(case.store, case.config, kind="worker", issue=9, wait_s=0):
            pytest.fail("A different issue cannot use issue 8's reservation")


# Defect class: liveness applied to siblings but not to this store's own runs.
def test_dead_own_run_and_expired_pidless_own_run_hold_nothing(case, monkeypatch):
    case.table.add(42002, cwd=case.root, start_time=1)
    case.store.start_run(5, branch="b", worktree="w", pid=42002)
    case.table.exit(42002)
    with capacity.hold_workload(case.store, case.config, wait_s=0):
        pass
    case.store.finish_run(5, "merged")
    case.store.start_run(6, branch="b", worktree="w")
    with pytest.raises(TimeoutError):
        with capacity.hold_workload(case.store, case.config, wait_s=0):
            pytest.fail("A pid-less run inside its launch grace still holds its slot")
    monkeypatch.setattr(capacity, "LAUNCH_GRACE", timedelta(0))
    with capacity.hold_workload(case.store, case.config, wait_s=0):
        pass
    assert case.store.get_run(6).status == "running", "Admission never changes run records"


# Defect class: an unbounded wait inside a bounded acquisition.
def test_machine_lock_wait_is_bounded(case, monkeypatch, capsys):
    clock = fake_clock(monkeypatch)
    lock = (case.root / capacity.LOCK_FILE).open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with capacity.hold_workload(case.store, case.config, wait_s=0):
                pytest.fail("A tick cannot wait for another gate's lock")
        assert clock[0] == 0
        with pytest.raises(TimeoutError, match="lock"):
            with capacity.hold_workload(case.store, case.config, wait_s=5):
                pytest.fail("The lock never freed")
        assert clock[0] == 5
    finally:
        lock.close()
    assert capacity_sessions.read(case.store.root) == []
    assert "Ctrl-C" in capsys.readouterr().err


def test_test_slot_guard_lock_wait_is_bounded(case, monkeypatch):
    clock = fake_clock(monkeypatch)
    lock = (case.root / capacity.TEST_SLOTS_LOCK).open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with capacity.hold_workload(case.store, case.config, tests=True, wait_s=3):
                pytest.fail("The guard lock never freed")
    finally:
        lock.close()
    assert clock[0] == 3
    assert capacity.machine_runs(case.store, now()) == 0


# Defect class: each stage restarting its own timeout.
def test_one_wait_budget_covers_machine_and_test_slot(case, monkeypatch):
    clock = fake_clock(monkeypatch, step=1)
    sibling = StateStore(case.root / "other")
    sibling.start_run(99, branch="b", worktree="w", pid=capacity.os.getpid())
    original = capacity.time.sleep

    def sleep(seconds):
        original(seconds)
        if clock[0] >= 4 and sibling.active_runs():
            sibling.finish_run(99, "merged")

    monkeypatch.setattr(capacity.time, "sleep", sleep)
    held = capacity._take_test_slot(1)
    try:
        with pytest.raises(TimeoutError):
            with capacity.hold_workload(case.store, case.config, tests=True, wait_s=6):
                pytest.fail("No test slot was free")
    finally:
        held.close()
    assert 6 <= clock[0] < 7, "The test slot only gets what is left of the one wait budget"


# Defect class: guidance missing from one admission stage.
def test_test_slot_wait_and_timeout_speak_the_selected_language(case, monkeypatch, capsys):
    from dataclasses import replace  # noqa: F401

    case.config.language = "ko"
    fake_clock(monkeypatch, step=1)
    held = capacity._take_test_slot(1)
    try:
        with pytest.raises(TimeoutError) as error:
            with capacity.hold_workload(case.store, case.config, tests=True, wait_s=3):
                pytest.fail("No test slot was free")
    finally:
        held.close()
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert "Ctrl-C" in lines[0] and "1개 중 1개 사용" in lines[0] and "0개 남음" in lines[0]
    assert "owner__project workload" in lines[0], "Names the Foragent work running"
    assert lines[1] == str(error.value) and any("가" <= ch <= "힣" for ch in lines[1])


# Defect class: ownership inherited through a copied context mistaken for synchronous nesting.
def reviewers(case, count, *, tests=False, wait_s=0):
    """Run reviewers as run_review does: a pool thread per reviewer, each in copy_context()."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    import threading

    inside, done = threading.Barrier(count + 1), threading.Event()

    def review():
        with capacity.hold_workload(case.store, case.config, kind="reviewer", tests=tests, wait_s=wait_s):
            inside.wait(timeout=5)
            done.wait(timeout=5)

    pool = ThreadPoolExecutor(max_workers=count)
    futures = [pool.submit(contextvars.copy_context().run, review) for _ in range(count)]
    return pool, futures, inside, done


def test_copied_context_reviewers_each_need_their_own_slot(case):
    with capacity.hold_workload(case.store, case.config, kind="worker"):
        pool, futures, _, done = reviewers(case, 1)
        error = futures[0].exception(timeout=5)
        done.set()
        pool.shutdown()
        assert isinstance(error, TimeoutError), "At budget 1 the worker's slot is not the reviewer's"
        assert capacity.machine_runs(case.store, now()) == 1


def test_copied_context_reviewers_count_one_slot_each(case):
    capacity.write_state(case.store, 3, now())
    with capacity.hold_workload(case.store, case.config, kind="worker"):
        pool, futures, inside, done = reviewers(case, 2, wait_s=5)
        inside.wait(timeout=5)
        assert capacity.machine_runs(case.store, now()) == 3
        done.set()
        pool.shutdown()
        assert [future.exception() for future in futures] == [None, None]
        assert capacity.machine_runs(case.store, now()) == 1


def test_copied_context_reviewer_takes_its_own_test_slot(case):
    capacity.write_state(case.store, 3, now())
    with capacity.hold_workload(case.store, case.config, kind="worker", tests=True):
        pool, futures, _, done = reviewers(case, 1, tests=True)
        error = futures[0].exception(timeout=5)
        done.set()
        pool.shutdown()
        assert isinstance(error, TimeoutError), "The one test slot belongs to the worker's thread"
        assert capacity.machine_runs(case.store, now()) == 1
