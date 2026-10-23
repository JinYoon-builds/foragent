"""Acceptance test for test-slots. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
import shlex
import sys
from threading import Event
import time

import pytest
import yaml

from conftest import ACCEPTANCE, GitHistory, git_command
from foragent.config import Config, load_config
from test_fast_checks import fast_case
from test_orch_review_handoff import bind_cli, tick_context
from test_skills_autosync import cli_exit, isolated_home
from test_state_split import offline_acceptance


@dataclass(frozen=True)
class SlotConfig(Config):
    """Supply the proposed input before Config supports it; loading is tested separately."""

    test_slots: int = 2


def slot_case(history, directory, monkeypatch, *, slots=1, repo="owner/repo", **settings):
    from foragent import runner

    directory.mkdir(parents=True, exist_ok=True)
    case = fast_case(history, directory, monkeypatch, {
        "src/foragent/scheduler.py": "def tick(ctx):\n    return (ctx, 'changed')\n",
    })
    case.check_process = runner.run_logged
    case.config = case.ctx.config = SlotConfig(**{
        **asdict(case.config), "max_parallel": 8, "test_slots": slots, "repo": repo,
        **settings,
    })
    case.ctx.repo = repo
    case.ctx.executor_factory = lambda name: (
        case.implementer if name == case.config.executor else case.reviewer
    )
    if case.config.reviewer == "claude":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    return case


def two_cases(git_history, tmp_path, monkeypatch):
    first = slot_case(git_history, tmp_path / "first", monkeypatch)
    directory = tmp_path / "second"
    directory.mkdir()
    origin, clone = directory / "origin.git", directory / "clone"
    git_command(directory, "init", "--bare", "--initial-branch=main", str(origin))
    history = GitHistory(origin, clone)
    history.update({ACCEPTANCE: "def test_feature():\n    assert True\n"})
    git_command(directory, "clone", str(origin), str(clone))
    second = slot_case(history, directory, monkeypatch, repo="owner/other")
    assert first.store.root != second.store.root
    assert first.ctx.repo != second.ctx.repo
    return first, second


def command_kind(case, command):
    words = shlex.split(command)
    if any(word.startswith("--junitxml=") for word in words):
        return "baseline-discovery"
    if command == case.done_commands[-1]:
        return "done-when"
    if case.baseline_ids.issubset(words):
        return "baseline-verification"
    if "tests/test_scheduler.py" in words:
        return "related-units"
    return "other"


class BlockedCheck:
    """Block only a check subprocess; leave run_issue and slot acquisition real."""

    def __init__(self, first, monkeypatch, second=None, *, kind="baseline-discovery"):
        from foragent import runner
        from foragent.processes import run_logged

        self.entered, self.release = Event(), Event()
        self.second_checking, self.second_started = Event(), Event()
        self.order = []
        cases = {first.store.log_path(1): first}
        if second is not None:
            cases[second.store.log_path(1)] = second
            update = second.store.update_run

            def observe(number, **fields):
                result = update(number, **fields)
                if fields.get("phase") == "checking":
                    self.second_checking.set()
                return result

            monkeypatch.setattr(second.store, "update_run", observe)
        original = run_logged

        def process(argv, *, cwd, **kwargs):
            case = cases.get(Path(kwargs["log_path"]))
            if case is None:
                return original(argv, cwd=cwd, **kwargs)
            assert argv[:2] == ["/bin/sh", "-c"], argv
            if case is second and not self.second_started.is_set():
                self.order.append("second:start")
                self.second_started.set()
            if case is first and command_kind(case, argv[2]) == kind and not self.entered.is_set():
                self.order.append("first:start")
                self.entered.set()
                assert self.release.wait(15), "test did not release the fake check subprocess"
                result = case.check_process(argv, cwd=cwd, **kwargs)
                self.order.append("first:end")
                return result
            return case.check_process(argv, cwd=cwd, **kwargs)

        monkeypatch.setattr(runner, "run_logged", process)


def merged(future, case):
    outcome = future.result(timeout=20)
    assert outcome == "merged", (outcome, case.store.log_path(1).read_text())
    assert len(case.tracker.merged_prs) == 1


@pytest.mark.parametrize("configured,expected", [(None, 2), (1, 1), (4, 4)])
def test_test_slots_config_is_separate_from_worker_ceiling(tmp_path, configured, expected):
    settings = {"max_parallel": 8, "skill_libraries": []}
    if configured is not None:
        settings["test_slots"] = configured
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump(settings))
    config = load_config(tmp_path)
    assert getattr(config, "test_slots", None) == expected, "Config must expose test_slots (default 2)"
    assert config.max_parallel == 8
    assert getattr(Config(), "test_slots", None) == 2


@pytest.mark.parametrize("kind", [
    "baseline-discovery", "done-when", "baseline-verification", "related-units",
])
def test_test_slots_serialize_runner_checks_across_repositories(
    git_history, tmp_path, monkeypatch, kind,
):
    from foragent.runner import run_issue

    first, second = two_cases(git_history, tmp_path, monkeypatch)
    blocked = BlockedCheck(first, monkeypatch, second, kind=kind)
    if kind == "baseline-discovery":
        implement = first.implementer.steps[0]

        def remote_thinking(kwargs):
            # A slot belongs to the command, not the worker's whole lifetime.
            assert blocked.second_started.wait(10), "remote thinking retained the only test slot"
            return implement(kwargs)

        first.implementer.steps = [remote_thinking]
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(run_issue, 1, first.ctx)
        try:
            assert blocked.entered.wait(10), first.store.log_path(1).read_text()
            waiter = pool.submit(run_issue, 1, second.ctx)
            assert blocked.second_checking.wait(10), second.store.log_path(1).read_text()
            overlap = blocked.second_started.wait(0.5)
        finally:
            blocked.release.set()
        merged(holder, first)
        merged(waiter, second)
    assert not overlap, f"second test command started while {kind} held the only machine-wide slot"
    assert blocked.order.index("first:end") < blocked.order.index("second:start"), blocked.order


def test_test_slots_wait_exceeds_baseline_and_run_limits_without_spending_budget(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    first, second = two_cases(git_history, tmp_path, monkeypatch)
    assert second.config.max_runtime_minutes == 90
    blocked = BlockedCheck(first, monkeypatch, second)
    started_at = second.clock[0]
    waited = 90 * 60 + 1
    calls = []
    check = second.check_process

    def timed_check(argv, *, cwd, **kwargs):
        if command_kind(second, argv[2]) == "baseline-discovery":
            calls.append(kwargs["timeout_s"])
            # This is execution time, distinct from the much longer queue wait.
            second.clock[0] += timedelta(seconds=7)
        return check(argv, cwd=cwd, **kwargs)

    second.check_process = timed_check
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(run_issue, 1, first.ctx)
        try:
            assert blocked.entered.wait(10)
            waiter = pool.submit(run_issue, 1, second.ctx)
            assert blocked.second_checking.wait(10)
            blocked.second_started.wait(0.5)
            second.clock[0] += timedelta(seconds=waited)
        finally:
            blocked.release.set()
        merged(holder, first)
        merged(waiter, second)
    run = second.store.get_run(1)
    assert run.waited_seconds == waited, "slot waiting must be credited using the store clock"
    assert run.executor_waited_seconds == 0
    assert (second.clock[0] - started_at).total_seconds() == waited + 7
    assert calls == [900], "queue time must not reduce the baseline's 900-second execution timeout"
    assert second.implementer.calls[0]["timeout_s"] == 90 * 60 - 7
    assert "Acceptance baseline count=2, exit_code=1, timed_out=False" in second.store.log_path(1).read_text()
    assert second.store.load_flag(1) is None


@pytest.mark.parametrize("executor,reviewer", [("claude", "codex"), ("codex", "claude")])
def test_test_slots_implementer_prompt_names_queued_test_command(
    git_history, tmp_path, monkeypatch, executor, reviewer,
):
    from foragent.runner import run_issue

    case = slot_case(git_history, tmp_path, monkeypatch, executor=executor, reviewer=reviewer)
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert "fa test" in case.implementer.calls[0]["prompt"], "implementers need the test-slot entry point"


def test_test_slots_every_generated_lane_agent_names_queued_test_command(
    git_history, monkeypatch,
):
    from foragent.cli import main
    from foragent.git import GitRepo

    root = git_history.clone
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\n")
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr(GitRepo, "push_base", lambda *a, **kw: None)
    for name in ("api", "web", "docs"):
        assert main(["lanes", "add", name, "--paths", f"{name}/**", "--repo-root", str(root)]) == 0
    agents = sorted((root / ".claude/agents").glob("fa-*.md"))
    assert len(agents) == 3
    missing = [path.name for path in agents if "fa test" not in path.read_text()]
    assert not missing, f"lane agents do not tell workers how to queue tests: {missing}"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_test_slots_cli_waits_for_runner_then_forwards_pytest_args_output_and_exit(
    git_history, tmp_path, monkeypatch, capfd, exit_code,
):
    from foragent.runner import run_issue

    case = slot_case(git_history, tmp_path, monkeypatch, test_command="pytest")
    main = bind_cli(case, monkeypatch)
    started = tmp_path / "pytest-started.json"
    binary = tmp_path / "bin/pytest"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(started)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('queued pytest stdout', flush=True)\n"
        "print('queued pytest stderr', file=sys.stderr, flush=True)\n"
        f"raise SystemExit({exit_code})\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    blocked = BlockedCheck(case, monkeypatch)
    invoked = Event()

    def invoke():
        invoked.set()
        return cli_exit(main, ["test", "--", "-q", "x"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(run_issue, 1, case.ctx)
        try:
            assert blocked.entered.wait(10)
            command = pool.submit(invoke)
            assert invoked.wait(10)
            deadline = time.monotonic() + 0.5
            while not started.exists() and not command.done() and time.monotonic() < deadline:
                blocked.release.wait(0.01)
            escaped = started.exists()
        finally:
            blocked.release.set()
        merged(holder, case)
        status = command.result(timeout=10)
    output = capfd.readouterr()
    assert status == exit_code, f"fa test must return pytest's exit code; got {status}: {output.err}"
    assert not escaped, "fa test started pytest while a runner held the only slot"
    assert json.loads(started.read_text()) == ["-q", "x"]
    assert "queued pytest stdout" in output.out
    assert "queued pytest stderr" in output.err


@pytest.mark.parametrize("pressure,load", [("normal", 2.0), ("warn", 0.5), ("critical", 0.5)])
def test_test_slots_tick_decreases_one_then_recovers_to_ceiling_in_gate_status(
    git_history, tmp_path, monkeypatch, capsys, pressure, load,
):
    from foragent import scheduler

    case = slot_case(git_history, tmp_path, monkeypatch, slots=3)
    main = bind_cli(case, monkeypatch)
    signal = [pressure, load]
    samples = []

    def machine_load():
        samples.append(tuple(signal))
        return tuple(signal)

    # #106 names this public sensor, but it is not merged in the baseline yet.
    monkeypatch.setattr(scheduler, "machine_load", machine_load, raising=False)
    context = tick_context(case, start_runs=False)

    def tick_slots(expected):
        case.clock[0] += timedelta(minutes=10)
        before = len(samples)
        assert scheduler.tick(context).skipped is None
        assert main(["gate", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        actual = payload.get("test_slots")
        assert type(actual) is int and actual == expected, (
            f"gate status test_slots must report effective capacity {expected}; got {actual!r}"
        )
        assert len(samples) > before, "tick must consume #106's machine_load signal"

    for expected in (2, 1, 1):
        tick_slots(expected)
    # Worker capacity still follows #106's multiplicative decrease, independently.
    assert scheduler.next_parallel(4, pressure=pressure, load_per_cpu=load, lanes_with_work=8,
                                   ceiling=8, increase_ok=True) == 2
    signal[:] = ["normal", 0.5]
    for expected in (2, 3, 3):
        tick_slots(expected)
