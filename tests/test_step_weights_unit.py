"""Phase-boundary resource accounting with the shared fake process table."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest

from foragent import capacity, orch_facts, step_weights, step_weights_usage as usage
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 11, 4, tzinfo=UTC)
GIB = 1024**3
NATIVE_SELF_USAGE = usage._self_usage


@pytest.fixture
def measured(tmp_path, monkeypatch, fake_process_table):
    table = fake_process_table
    monkeypatch.setattr(capacity, 'machine_root', lambda: tmp_path / 'machine')
    monkeypatch.setattr(usage, '_self_usage', NATIVE_SELF_USAGE)
    clock, metrics = [NOW], {}
    root_pid = 60000
    store = StateStore(tmp_path / 'data' / 'owner__project', now=lambda: clock[0])

    def add(pid, *, parent=1, memory=.4 * GIB):
        birth = clock[0].timestamp()
        table.add(pid, ppid=parent, cwd=tmp_path, start_time=birth)
        metrics[pid, birth] = {'own': 0., 'children': 0., 'memory': int(memory),
                               'peak': int(memory), 'children_peak': 0}
        return pid

    class Process:
        def __init__(self, pid):
            if pid not in table.live:
                raise psutil.NoSuchProcess(pid)
            self.pid, self.birth = pid, table.rows[pid].start_time

        def create_time(self):
            return table.rows[self.pid].start_time

        def ppid(self):
            return table.rows[self.pid].ppid

        def status(self):
            return psutil.STATUS_RUNNING

        def children(self, recursive=False):
            result, frontier = [], [self.pid]
            while frontier:
                parent = frontier.pop()
                kids = [pid for pid in table.live if table.rows[pid].ppid == parent]
                result.extend(Process(pid) for pid in kids)
                if recursive:
                    frontier.extend(kids)
            return result

        def cpu_times(self):
            row = metrics[self.pid, self.birth]
            return SimpleNamespace(user=row['own'], system=0., children_user=row['children'], children_system=0.)

        def memory_info(self):
            row = metrics[self.pid, self.birth]
            return SimpleNamespace(rss=row['memory'])

    monkeypatch.setattr(psutil, 'Process', Process)
    add(root_pid)
    store.start_run(1, branch='b', worktree=tmp_path / 'work', pid=root_pid)

    def row(pid=root_pid):
        return metrics[pid, table.rows[pid].start_time]

    def rusage(kind):
        own = kind == usage.resource.RUSAGE_SELF
        value = row()
        return SimpleNamespace(ru_utime=value['own'] if own else value['children'], ru_stime=0.,
                               ru_maxrss=value['peak'] if own else value['children_peak'])

    monkeypatch.setattr(usage, 'os', SimpleNamespace(getpid=lambda: root_pid))
    monkeypatch.setattr(usage, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(usage.resource, 'getrusage', rusage)

    def advance(seconds, *, child=None, child_cpu=0., root_cpu=.2):
        row()['own'] += seconds * root_cpu
        if child is not None:
            row(child)['own'] += seconds * child_cpu
        clock[0] += timedelta(seconds=seconds)

    def reap(pid):
        child, parent = row(pid), row(table.rows[pid].ppid)
        parent['children'] += child['own'] + child['children']
        parent['children_peak'] = max(parent['children_peak'], child['peak'], child['children_peak'])
        table.exit(pid)

    def phase(name):
        return store.update_run(1, phase=name, phase_started_at=clock[0].isoformat())

    return SimpleNamespace(store=store, table=table, clock=clock, row=row, add=add,
                           advance=advance, reap=reap, phase=phase, root=root_pid, path=tmp_path)


@pytest.mark.parametrize('phase', step_weights.STEPS)
def test_two_minute_phase_is_learned_at_its_end_without_a_tick(measured, phase):
    c = measured
    child = c.add(60001, parent=c.root, memory=6 * GIB)
    c.phase(phase)
    c.advance(120, child=child, child_cpu=6)
    c.reap(child)
    c.phase('ci-wait')
    weight = step_weights.learned(StateStore(c.store.root))[phase]
    assert weight['cpu_cores'] == pytest.approx(6.2)
    assert weight['memory_bytes'] == pytest.approx(6.4 * GIB)
    assert weight['cpu_samples'] == weight['memory_samples'] == 1
    assert not c.table.signals


def test_children_living_only_between_ticks_are_counted_in_every_step(measured):
    c = measured
    for phase in step_weights.STEPS:
        c.phase(phase)
        for _ in range(4):
            child = c.add(60001, parent=c.root, memory=6 * GIB)
            c.advance(250, child=child, child_cpu=6, root_cpu=0)
            c.reap(child)
            c.advance(50, root_cpu=0)
            step_weights.record(c.store)
            assert step_weights.learned(c.store)[phase]['cpu_cores'] == 5.
        c.phase('ci-wait')
        assert step_weights.learned(c.store)[phase]['cpu_samples'] == 1


@pytest.mark.parametrize('phase', step_weights.STEPS)
def test_reused_child_pid_keeps_departed_lifetime_cpu(measured, phase):
    c = measured
    child = c.add(60001, parent=c.root, memory=6 * GIB)
    recorded = c.store.update_run(1, observe=True)
    assert any(item['pid'] == child for item in recorded.process_identities)
    c.phase(phase)
    c.advance(8, child=child, child_cpu=6.25)
    c.reap(child)
    c.add(child, parent=c.root, memory=GIB)
    c.advance(2, child=child, child_cpu=5)
    c.phase('ci-wait')
    assert step_weights.learned(c.store)[phase]['cpu_cores'] == pytest.approx(6.2)


def test_live_and_reaped_descendants_are_not_double_counted(measured, monkeypatch):
    c = measured
    monkeypatch.setattr(usage, '_self_usage', lambda pid: None)  # Linux psutil counters, without self override.
    child = c.add(60001, parent=c.root)
    c.phase('checking')
    c.row()['own'], c.row()['children'] = 2., 30.
    c.row(child)['own'], c.row(child)['children'] = 10., 20.
    c.clock[0] += timedelta(seconds=10)
    c.phase('ci-wait')
    assert step_weights.learned(c.store)['checking']['cpu_cores'] == pytest.approx(6.2)


def test_reported_child_peak_survives_reaping_between_boundaries(measured):
    c = measured
    c.phase('reviewing')
    child = c.add(60001, parent=c.root, memory=GIB)
    c.row(child)['peak'] = 9 * GIB
    c.advance(120, child=child, child_cpu=1)
    c.reap(child)
    c.store.finish_run(1, outcome='merged')
    weight = step_weights.learned(c.store)['reviewing']
    assert weight['memory_bytes'] == pytest.approx(9.4 * GIB)
    assert weight['cpu_cores'] == pytest.approx(1.2)


def test_boundary_result_replaces_tick_prefix_and_survives_reload(measured):
    c = measured
    c.phase('implementing')
    c.advance(300, root_cpu=1)
    step_weights.record(c.store)
    c.advance(300, root_cpu=3)
    c.phase('ci-wait')
    weight = step_weights.learned(StateStore(c.store.root))['implementing']
    assert weight['cpu_cores'] == 2. and weight['cpu_samples'] == 1
    assert step_weights.learned(StateStore(c.store.root.with_name('another')))['implementing']['cpu_cores'] is None


def test_repeated_tick_does_not_add_an_observation(measured):
    c = measured
    c.phase('checking')
    c.advance(10)
    for _ in range(3):
        step_weights.record(c.store)
    assert step_weights.learned(c.store)['checking']['cpu_samples'] == 1


def test_root_pid_reuse_is_not_a_new_measurement_of_the_old_run(measured):
    c = measured
    c.phase('checking')
    c.advance(10)
    c.table.exit(c.root)
    c.add(c.root)
    c.row()['own'] = 999999.
    c.phase('ci-wait')
    assert step_weights.learned(c.store)['checking']['cpu_cores'] is None


def test_phase_start_counters_exclude_preceding_work(measured):
    c = measured
    c.phase('implementing')
    c.advance(600, root_cpu=6)
    c.phase('reviewing')
    c.advance(120, root_cpu=.2)
    c.phase('checking')
    c.advance(120, root_cpu=1)
    c.phase('ci-wait')
    weights = step_weights.learned(c.store)
    assert [weights[phase]['cpu_cores'] for phase in step_weights.STEPS] == pytest.approx([6., .2, 1.])


def test_facts_reads_persisted_measurements_without_process_probes(measured, monkeypatch):
    c = measured
    c.phase('checking')
    c.advance(120, root_cpu=2)
    c.store.finish_run(1, outcome='merged')
    before = step_weights.path_for(c.store).read_bytes()
    monkeypatch.setattr(psutil, 'Process', lambda *a: pytest.fail('facts probed a process'))
    result = orch_facts.collect(c.store, Config(), c.path)
    assert result['step_weights']['checking']['cpu_cores'] == 2.
    assert 'checking 2.00 CPU/0.40 GiB' in orch_facts.human(result)
    assert step_weights.path_for(c.store).read_bytes() == before


def test_write_error_keeps_the_last_accepted_measurement(measured, monkeypatch):
    c = measured
    c.phase('checking')
    c.advance(10)
    step_weights.record(c.store)
    before = step_weights.path_for(c.store).read_bytes()
    monkeypatch.setattr(step_weights.os, 'replace', lambda *a: (_ for _ in ()).throw(PermissionError()))
    c.advance(10)
    step_weights.record(c.store)
    assert step_weights.path_for(c.store).read_bytes() == before
    assert not list(step_weights.path_for(c.store).parent.glob('.step-weights-*'))


@pytest.mark.parametrize('platform,scale', [('darwin', 1), ('linux', 1024)])
def test_native_runner_uses_waited_children_rusage_on_macos(monkeypatch, platform, scale):
    monkeypatch.setattr(usage, 'os', SimpleNamespace(getpid=lambda: 123))
    monkeypatch.setattr(usage, 'sys', SimpleNamespace(platform=platform))
    readings = {usage.resource.RUSAGE_SELF: SimpleNamespace(ru_utime=1., ru_stime=1., ru_maxrss=4),
                usage.resource.RUSAGE_CHILDREN: SimpleNamespace(ru_utime=50., ru_stime=10., ru_maxrss=6)}
    monkeypatch.setattr(usage.resource, 'getrusage', readings.__getitem__)
    assert usage._self_usage(123) == (2., 60., 4 * scale, 6 * scale)
    assert usage._self_usage(124) is None


def test_phase_boundary_reads_native_rusage_when_psutil_has_zero_children(measured, monkeypatch):
    c = measured
    waited = {'cpu': 0., 'peak': 0}
    monkeypatch.setattr(usage, '_self_usage', NATIVE_SELF_USAGE)
    monkeypatch.setattr(usage, 'os', SimpleNamespace(getpid=lambda: c.root))
    monkeypatch.setattr(usage, 'sys', SimpleNamespace(platform='darwin'))

    def rusage(kind):
        own = kind == usage.resource.RUSAGE_SELF
        return SimpleNamespace(ru_utime=c.row()['own'] if own else waited['cpu'], ru_stime=0.,
                               ru_maxrss=.4 * GIB if own else waited['peak'])

    monkeypatch.setattr(usage.resource, 'getrusage', rusage)
    c.phase('checking')
    child = c.add(60001, parent=c.root, memory=6 * GIB)
    c.advance(120, child=child, child_cpu=6)
    c.reap(child)
    waited.update(cpu=720., peak=6 * GIB)
    c.row()['children'] = c.row()['children_peak'] = 0  # macOS psutil omits waited usage.
    c.store.finish_run(1, outcome='merged')
    weight = step_weights.learned(c.store)['checking']
    assert weight['cpu_cores'] == pytest.approx(6.2)
    assert weight['memory_bytes'] == pytest.approx(6.4 * GIB)


def test_gate_and_phase_measurements_share_the_actual_state_lock(measured, monkeypatch):
    from contextlib import contextmanager

    c = measured
    locked = []
    real_lock, real_capture = c.store._state_lock, usage.snapshot

    @contextmanager
    def tracked_lock():
        with real_lock():
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()

    def capture(*args):
        assert locked, 'resource reads must be serialized with phase transitions'
        return real_capture(*args)

    monkeypatch.setattr(c.store, '_state_lock', tracked_lock)
    monkeypatch.setattr(usage, 'snapshot', capture)
    c.phase('checking')
    c.advance(10, root_cpu=2)
    step_weights.record(c.store)
    c.phase('ci-wait')
    assert step_weights.learned(c.store)['checking']['cpu_cores'] == 2.


def test_a_day_of_short_reviews_and_checks_learns_all_steps_at_300_second_cadence(measured):
    c = measured
    for execution in range(96):
        if execution:
            c.table.exit(c.root)
            c.add(c.root)
            c.store.start_run(1, branch='b', worktree=c.path / 'work', pid=c.root)
        c.phase('implementing')
        for _ in range(2):
            c.advance(300)
            step_weights.record(c.store)
        c.phase('reviewing')
        c.advance(120)
        child = c.add(60001, parent=c.root, memory=6 * GIB)
        c.phase('checking')
        c.advance(120, child=child, child_cpu=6)
        c.reap(child)
        c.store.finish_run(1, outcome='merged')
        c.advance(60)
    assert c.clock[0] - NOW == timedelta(days=1)
    step_weights.record(c.store)
    result = step_weights.learned(StateStore(c.store.root))
    assert [result[phase]['cpu_cores'] for phase in step_weights.STEPS] == pytest.approx([.2, .2, 6.2])
    assert [result[phase]['memory_bytes'] / GIB for phase in step_weights.STEPS] == pytest.approx([.4, .4, 6.4])
    assert [result[phase]['cpu_samples'] for phase in step_weights.STEPS] == [96, 96, 96]
    assert not c.table.signals
