"""Acceptance test for capacity-aimd. Locked by Foragent; do not edit."""

import pytest

from test_skills_autosync import isolated_home


@pytest.mark.parametrize("current,work,ceiling,expected", [(2, 8, None, 3), (2, 2, None, 2), (2, 8, 2, 2)])
def test_capacity_aimd_headroom_adds_one_with_work_and_ceiling_caps(current, work, ceiling, expected):
    from foragent.scheduler import next_parallel

    assert next_parallel(current, pressure="normal", load_per_cpu=0.5, lanes_with_work=work,
                         ceiling=ceiling, increase_ok=True) == expected


@pytest.mark.parametrize("pressure,current,expected", [("warn", 7, 3), ("critical", 8, 4), ("critical", 1, 1)])
def test_capacity_aimd_pressure_halves_immediately_without_increase_permission(pressure, current, expected):
    from foragent.scheduler import next_parallel

    assert next_parallel(current, pressure=pressure, load_per_cpu=0.5, lanes_with_work=10,
                         ceiling=None, increase_ok=False) == expected


@pytest.mark.parametrize("load,increase_ok,expected", [(1.5, True, 3), (2.0, False, 3), (0.5, False, 6)])
def test_capacity_aimd_high_load_halves_and_normal_load_waits(load, increase_ok, expected):
    from foragent.scheduler import next_parallel

    assert next_parallel(6, pressure="normal", load_per_cpu=load, lanes_with_work=10,
                         ceiling=None, increase_ok=increase_ok) == expected


def test_capacity_aimd_config_defaults_to_no_ceiling_but_accepts_explicit_cap(tmp_path):
    from foragent.config import Config, load_config

    assert Config().max_parallel is None
    # A Foragent repo without a max_parallel key (an absent file is "not a repo").
    (tmp_path / ".foragent.yml").write_text("repo: owner/repo\n")
    assert load_config(tmp_path).max_parallel is None
    (tmp_path / ".foragent.yml").write_text("max_parallel: 3\n")
    config = load_config(tmp_path)
    assert config.max_parallel == 3
    from foragent.scheduler import next_parallel

    assert next_parallel(3, pressure="normal", load_per_cpu=0.5, lanes_with_work=8,
                         ceiling=config.max_parallel, increase_ok=True) == 3


def test_capacity_aimd_machine_load_smoke_returns_valid_pair():
    from foragent.scheduler import machine_load

    pressure, load_per_cpu = machine_load()
    assert pressure in {"normal", "warn", "critical"}
    assert isinstance(load_per_cpu, (int, float)) and load_per_cpu >= 0


@pytest.fixture
def machine_repositories(git_history, tmp_path, monkeypatch, isolated_home):
    """Independent gates sharing only HOME's machine data root and live pids."""
    from dataclasses import replace
    import os

    from conftest import git_command
    from foragent import scheduler
    from foragent.state import StateStore
    from test_orch_review_handoff import make_case, tick_context

    cases = []
    for name in ("a", "b"):
        directory = tmp_path / name
        directory.mkdir()
        clone = directory / "clone"
        git_command(directory, "clone", str(git_history.origin), str(clone))
        repo = f"owner/{name}"
        case = make_case(
            replace(git_history, clone=clone), directory, monkeypatch, repo=repo,
            max_parallel=4, skill_libraries=[], test_command="true",
            lanes={f"lane-{number}": {} for number in range(1, 9)},
        )
        # Replace make_case's private store before constructing either gate.
        case.store = case.ctx.store = StateStore.for_repo(repo, now=case.ctx.now)
        case.ctx.repo = repo
        assert case.store.root == isolated_home / ".local/share/foragent" / f"owner__{name}"
        body = case.tracker.get_issue(1).body
        case.tracker.remove_labels(1, ["running", "approved"])
        for number in range(1, 9):
            if number != 1:
                case.tracker.add_issue(number, f"Task {number}", body)
            case.tracker.add_labels(number, [f"lane:lane-{number}"])
            case.tracker.record_label(number, "approved", "human")
        case.launched = []

        def launch(number, case=case):
            case.launched.append(number)
            return os.getpid()

        case.tick_ctx = replace(tick_context(case), repo=repo, launcher=launch)
        cases.append(case)

    # #106's public sensor is absent in the baseline; tick must consume it.
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5), raising=False)
    return tuple(cases)


def occupy_machine_slots(case, numbers, *, pid):
    for number in numbers:
        case.store.start_run(
            number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number),
            lane=f"lane-{number}", pid=pid,
        )
        case.tracker.add_labels(number, ["running"])


def finish_machine_slots(case, numbers):
    for number in numbers:
        case.store.finish_run(number, outcome="merged")
        case.tracker.close_issue(number)
        case.tracker.remove_labels(number, ["running", "ready"])


def test_capacity_aimd_machine_budget_blocks_other_repo_until_one_run_finishes(
    machine_repositories, monkeypatch,
):
    import os

    from foragent import scheduler
    from foragent.state import StateStore

    first, second = machine_repositories
    # max_parallel=4 is a ceiling, not the current AIMD value of 2.
    monkeypatch.setattr(scheduler, "next_parallel", lambda current, **inputs: 2, raising=False)
    occupy_machine_slots(first, (1, 2), pid=os.getpid())

    report = scheduler.tick(second.tick_ctx)
    assert report.skipped is None, report
    assert second.launched == [], (
        f"Repo B started {second.launched} while repo A occupied both machine slots"
    )
    assert second.store.active_runs() == []
    assert all("ready" in second.tracker.get_issue(n).labels for n in range(1, 9))

    finish_machine_slots(first, (2,))
    # A fresh store must observe A's completion without an A tick.
    second.tick_ctx.store = StateStore.for_repo(second.ctx.repo, now=second.ctx.now)
    report = scheduler.tick(second.tick_ctx)
    assert report.skipped is None, report
    assert len(second.launched) == 1, "Exactly one machine slot became free"
    assert len(first.store.active_runs()) == len(second.store.active_runs()) == 1
    assert second.store.active_runs()[0].pid == os.getpid()


def test_capacity_aimd_machine_value_halved_in_one_repo_is_used_by_the_other(
    machine_repositories, monkeypatch,
):
    import os

    from foragent import scheduler

    first, second = machine_repositories
    for case in (first, second):
        occupy_machine_slots(case, (1, 2, 3, 4), pid=os.getpid())
    # Seed both gates through the public reducer, without specifying a state
    # filename, initial AIMD value, or additive-increase interval.
    with monkeypatch.context() as seed:
        seed.setattr(scheduler, "next_parallel", lambda current, **inputs: 4, raising=False)
        for case in (first, second):
            assert scheduler.tick(case.tick_ctx).skipped is None
            assert case.launched == []

    machine_root = first.store.root.parent
    before = {path.name: path.read_bytes() for path in machine_root.iterdir() if path.is_file()}
    finish_machine_slots(first, (3, 4))
    finish_machine_slots(second, (1, 2, 3, 4))
    # B still has four ready lanes: its old value of 4 cannot be capped to 2
    # just by lanes_with_work, which would hide a repository-local AIMD value.
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 2.0), raising=False)

    report = scheduler.tick(first.tick_ctx)
    assert report.skipped is None, report
    assert first.launched == [], (
        f"High machine load must halve the budget from 4 to 2 before launching; started {first.launched}"
    )
    assert any(
        path.is_file() and path.read_bytes() != before.get(path.name)
        for path in machine_root.iterdir()
    ), "The changed AIMD value must persist directly under the Foragent data root"

    # Exercise the real reducer while suppressing only additive increases:
    # D60 deliberately leaves their interval unspecified.
    next_parallel = scheduler.next_parallel

    def hold_increases(current, **inputs):
        return next_parallel(current, **{**inputs, "increase_ok": False})

    monkeypatch.setattr(scheduler, "next_parallel", hold_increases)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    report = scheduler.tick(second.tick_ctx)
    assert report.skipped is None, report
    assert second.launched == [], "Repo B must use A's halved budget of 2, not its previous value of 4"

    finish_machine_slots(first, (2,))
    report = scheduler.tick(second.tick_ctx)
    assert report.skipped is None, report
    assert len(second.launched) == 1, "The shared halved budget must still admit one free slot"
    assert len(first.store.active_runs()) == len(second.store.active_runs()) == 1


def test_capacity_aimd_machine_budget_counts_live_runs_but_ignores_stale_repo_runs(
    machine_repositories, monkeypatch,
):
    import os
    import subprocess
    import sys

    from foragent import scheduler

    first, second = machine_repositories
    monkeypatch.setattr(scheduler, "next_parallel", lambda current, **inputs: 2, raising=False)
    occupy_machine_slots(first, (1,), pid=os.getpid())
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.wait(timeout=10)
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    occupy_machine_slots(first, (2,), pid=child.pid)

    report = scheduler.tick(second.tick_ctx)
    assert report.skipped is None, report
    assert len(second.launched) == 1, (
        f"Budget 2 minus A's one live run leaves one slot; its dead pid must not count: {second.launched}"
    )
    assert len(second.store.active_runs()) == 1
    assert second.store.active_runs()[0].pid == os.getpid()
    report = scheduler.tick(second.tick_ctx)
    assert report.skipped is None, report
    assert len(second.launched) == 1, "The live runs in A and B now fill the machine budget"
