"""Acceptance test for wake-on-work: start the next gate pass as soon as work becomes possible.

A "kick" is the existing helper `foragent.kick.kick_tick(repo_root)`. Every caller,
however it imports the helper, ends in that function's launch step; these tests
replace only the launch step (`kick._launchd_kickstart`, the launchd start of the
installed gate job) with an offline recorder. No process starts and no signal is
sent. A "pass" is one real `fa gate tick` that holds the tick lock; passes are
counted by the tick's own recovery step, which every pass runs.

A launched gate job really starts at once: a kick that arrives while a pass holds
the tick lock starts a tick that finds the lock busy. The recorder reproduces that
by running the real `fa gate tick` immediately, on another thread (a separate
process holds a separate lock descriptor). A kick sent when no pass is running is
queued and run after the current call returns, one after another, so passes never
overlap. The five-minute timer is never simulated: only kicks start extra passes.
"""

from datetime import timedelta
import re
import threading
from types import SimpleNamespace

import pytest

from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)
from test_order import offline_publication  # noqa: F401 (fixture)
from test_order_publications import case  # noqa: F401 (fixture)

MAX_DRAINED_PASSES = 10


def record_kicks(monkeypatch, on_launch=None):
    """Record every kick that reaches the gate job's launch step; never start a process."""
    from foragent import cli, kick, orch, order, runner

    kicks = []
    # tests/conftest.py replaces these modules' kick_tick with a list for every test;
    # here every caller must reach the real helper, so its launch step is the only fake.
    for module in (cli, orch, order, runner):
        monkeypatch.setattr(module, "kick_tick", kick.kick_tick)

    def kickstart(repo):
        kicks.append(repo)
        if on_launch is not None:
            on_launch(repo)
        return True  # launchd accepted the start: no direct Popen fallback.

    monkeypatch.setattr(kick, "_launchd_kickstart", kickstart)
    return kicks


class Gate:
    """Real `fa gate tick` passes plus the kicks they and the test send."""

    def __init__(self, case, monkeypatch, capsys):
        self.case, self.capsys = case, capsys
        self.during_pass = False
        self.queued = 0
        self.busy_ticks = []
        self.kicks = record_kicks(monkeypatch, self._launch)

    def _launch(self, repo):
        if self.during_pass:
            # The launched job starts now and meets the running pass's tick lock.
            outcome = {}
            thread = threading.Thread(target=lambda: outcome.update(code=self._invoke()))
            thread.start()
            thread.join(60)
            assert not thread.is_alive(), "A tick launched during a pass must not wait for the lock"
            self.busy_ticks.append(outcome.get("code"))
        else:
            self.queued += 1

    def _invoke(self):
        from foragent.state import StateStore

        # A fresh store per invocation models launchd's separate processes.
        self.case.store = StateStore(self.case.store.root, now=lambda: self.case.clock[0])
        return cli_exit(self.case.main, ["gate", "tick", "--no-start", "--json",
                                         "--repo-root", str(self.case.root)])

    def passes(self):
        return len(self.case.tick_calls)

    def timer_pass(self):
        """One timer pass, then every pass that kicks start before the timer could fire again."""
        self.case.clock[0] += timedelta(minutes=5)
        before = self.passes()
        self.queued = 0
        assert self._invoke() == 0
        drained = 0
        while self.queued:
            self.queued -= 1
            drained += 1
            assert drained <= MAX_DRAINED_PASSES, "Passes keep starting passes: a loop"
            assert self._invoke() == 0
        self.capsys.readouterr()
        return self.passes() - before


@pytest.fixture
def gate(tick_case, monkeypatch, capsys):  # noqa: F811
    from foragent import scheduler

    case = tick_case
    # Kicks requested from inside the next pass; set by each test.
    case.kicks_during_pass = 0
    recover = scheduler._recover_runs
    gate = Gate(case, monkeypatch, capsys)

    def recovery_step(ctx, report):
        count, case.kicks_during_pass = case.kicks_during_pass, 0
        if count:
            from foragent.kick import kick_tick

            gate.during_pass = True
            try:
                for _ in range(count):
                    kick_tick(case.root)
            finally:
                gate.during_pass = False
        return recover(ctx, report)

    monkeypatch.setattr(scheduler, "_recover_runs", recovery_step)
    return gate


@pytest.mark.parametrize("kicks", [1, 3])
def test_kicks_during_a_running_pass_make_exactly_one_more_pass_right_after_it(gate, kicks):
    gate.case.kicks_during_pass = kicks
    passes = gate.timer_pass()
    assert len(gate.busy_ticks) + gate.kicks.count("owner/repo") >= 1
    assert passes == 2, (
        f"{kicks} kick(s) during a running pass must make exactly one more pass as soon as it ends, "
        f"not {passes - 1}; kicks seen: {gate.kicks}"
    )


def test_the_latched_kick_is_used_once_and_an_idle_pass_starts_nothing_by_itself(gate):
    gate.case.kicks_during_pass = 2
    assert gate.timer_pass() == 2, "kicks during a pass must make one more pass"
    # The extra pass made no progress: no third pass, and the latch is spent.
    assert gate.timer_pass() == 1, "A pass without progress or kicks must not start another pass"
    assert gate.timer_pass() == 1


# --- The merge owner asks for the next pass after its own progress -------------------------


def owner_kicks(monkeypatch, store):
    """Kicks, each with whether a gate pass started now could start the merge owner again."""
    kicks = []

    def launch(repo):
        lock = store.lock_acceptance_baseline("merge-owner", blocking=False)
        kicks[-1] = (repo, lock is not None)
        if lock is not None:
            lock.close()

    record_kicks(monkeypatch, lambda repo: (kicks.append(None), launch(repo)))
    return kicks


def run_owner_process(monkeypatch, store, config, root, tracker, git):
    """The real owner process entry point, holding the owner lock the way merge_owner.start hands it over."""
    from dataclasses import asdict
    import os

    from foragent import merge_owner, runner

    lock = store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert lock is not None, "no other owner may hold the lock between passes"
    descriptor = os.dup(lock.fileno())
    lock.close()  # Like merge_owner.start: only the owner process keeps the reservation.
    context = runner.RunContext

    def offline_context(repo, repo_root, _tracker, _store, owner_config, _git, now, **kwargs):
        # Only the transports are offline: GitHub, the process's own store object and Git.
        return context(repo, repo_root, tracker, store, owner_config, git, now, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runner, "RunContext", offline_context)
        merge_owner.run_process({"repo": config.repo, "repo_root": str(root), "store_root": str(store.root),
                                 "config": asdict(config), "committed_config": False}, descriptor)


def drain_owner(monkeypatch, store, config, root, tracker, git, kicks, limit=6):
    """Run owner passes while the previous one asked for a pass that can start the owner.

    Returns the kicks of each pass. The timer never runs, so work that needs a further
    pass is reached only through such a kick.
    """
    per_pass = []
    while True:
        assert len(per_pass) < limit, f"Owner passes keep asking for passes: {per_pass}"
        before = len(kicks)
        run_owner_process(monkeypatch, store, config, root, tracker, git)
        per_pass.append(kicks[before:])
        if not any(free for _, free in per_pass[-1]):
            return per_pass


def test_owner_publishing_a_queued_order_starts_the_next_pass_for_the_next_publication(case, monkeypatch):
    from foragent import order, order_publications

    (case.root / ".foragent.yml").write_text("repo: owner/repo\n", encoding="utf-8")
    order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo, git=case.git, approve=True)
    case.plan.update(id="second", title="Second", issues=[])
    order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo, git=case.git, approve=True)
    assert [row["status"] for row in order_publications.requests(case.store)] == ["pending", "pending"]
    # Also restores the publication kick the shared fixture silences.
    kicks = owner_kicks(monkeypatch, case.store)

    per_pass = drain_owner(monkeypatch, case.store, case.config, case.root, case.tracker, case.git, kicks)

    statuses = [row["status"] for row in order_publications.requests(case.store)]
    assert statuses == ["published", "published"], (
        f"After publishing one queued order the owner must start the next pass without the timer: "
        f"{statuses}, kicks per owner pass: {per_pass}"
    )
    assert all(repo == "owner/repo" for kicked in per_pass for repo, _ in kicked), per_pass
    # The owner applies every pending publication in one pass (main a15d8bf3): one publishing pass, one idle pass.
    assert len(per_pass) == 2, f"One publishing pass and one idle pass, not {per_pass}"
    assert per_pass[-1] == [], "An owner pass that published nothing must not ask for another pass"


@pytest.fixture
def main_write_owner(offline_publication, tmp_path, monkeypatch):  # noqa: F811
    from dataclasses import replace

    from foragent.state import StateStore

    root, config, tracker, git, calls, origin = offline_publication
    with (root / ".foragent.yml").open("a", encoding="utf-8") as stream:
        stream.write("repo: owner/repo\n")
    store = StateStore(tmp_path / "owner-state")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    config = replace(config, repo="owner/repo", merge_owner=True, skill_libraries=[])
    return SimpleNamespace(root=root, config=config, tracker=tracker, git=git, store=store, origin=origin)


def test_owner_applying_main_write_requests_starts_the_next_pass_and_an_idle_owner_does_not(
    main_write_owner, monkeypatch,
):
    from conftest import git_command
    from foragent import main_write_requests

    case = main_write_owner
    name = ".claude/agent-memory/fa-docs/MEMORY.md"
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"A lesson for the docs lane.\n")
    assert main_write_requests.capture(case.git, case.config, [name], "Memory", "lane docs", store=case.store)
    kicks = owner_kicks(monkeypatch, case.store)

    run_owner_process(monkeypatch, case.store, case.config, case.root, case.tracker, case.git)
    assert main_write_requests.pending(case.store) == [], "the owner pass must apply the request"
    assert git_command(case.origin, "show", f"main:{name}") == "A lesson for the docs lane."
    applied = list(kicks)
    assert any(free for _, free in applied), (
        f"After applying a main-write request the owner must start the next gate pass at a moment "
        f"that pass can start the owner again; kicks: {applied}"
    )
    assert {repo for repo, _ in applied} == {"owner/repo"}, applied

    run_owner_process(monkeypatch, case.store, case.config, case.root, case.tracker, case.git)
    assert kicks[len(applied):] == [], "An owner pass without progress must not start another pass"


# --- Orchestrator commands and role sessions start a pass promptly -------------------------


@pytest.fixture
def local_project(tmp_path, monkeypatch, isolated_home, capsys):  # noqa: F811
    from foragent import kick
    from test_order_progress import offline_local_cli

    case = offline_local_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    case.kicks = record_kicks(monkeypatch)
    # Harness control: a kick from this project's root reaches the recorder with its repo.
    kick.kick_tick(case.root)
    assert case.kicks == ["owner/repo"]
    case.kicks.clear()

    def invoke(*argv):
        code = cli_exit(case.main, [*map(str, argv), "--repo-root", str(case.root)])
        output = capsys.readouterr()
        assert code == 0, f"fa {' '.join(map(str, argv))} must succeed (exit={code}):\n{output.err}"
        return output.out

    case.invoke = invoke
    return case


@pytest.mark.parametrize("change", [
    ["orch", "queue", "--global", "--add", 7],
    ["orch", "queue", "--global", 7, 8],
    ["orch", "queue", "main", 5, 6],
], ids=["global-add", "global-replace", "lane"])
def test_changing_the_priority_queue_starts_a_pass(local_project, change):
    case = local_project
    case.invoke(*change)
    assert case.kicks and set(case.kicks) == {"owner/repo"}, (
        f"fa {' '.join(map(str, change))} changes the priority queue and must start a pass: {case.kicks}"
    )


def test_releasing_the_project_stop_starts_a_pass(local_project):
    case = local_project
    case.invoke("stop", "--why", "Pause for a base repair")
    case.kicks.clear()
    case.invoke("orch", "release", "--why", "Base repaired")
    assert case.kicks and set(case.kicks) == {"owner/repo"}, (
        f"fa orch release lets held lanes start again and must start a pass: {case.kicks}"
    )


@pytest.mark.parametrize("exit_code", [0, 7])
def test_a_finished_role_session_starts_a_pass(local_project, monkeypatch, exit_code):
    from foragent import executors
    from foragent.executors import ExecResult

    case = local_project
    (case.store.root / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(executors, "executor_for", lambda name: SimpleNamespace(
        run=lambda **kw: ExecResult(exit_code, False, "Reviewed the structure.", kw["log_path"])))
    code = cli_exit(case.main, ["gate", "run-architect", "--repo-root", str(case.root),
                                "--started-at", "2026-11-06T10:00:00+00:00", "--why", "An order finished"])
    assert code == exit_code
    assert case.kicks and set(case.kicks) == {"owner/repo"}, (
        f"A role session that finished (exit {exit_code}) must start a pass: {case.kicks}"
    )


def test_unparking_an_issue_starts_a_pass(git_history, tmp_path, monkeypatch, isolated_home):  # noqa: F811
    from test_orch_park import park_case

    case, main = park_case(git_history, tmp_path, monkeypatch)
    kicks = record_kicks(monkeypatch)
    assert cli_exit(main, ["orch", "park", "1", "--why", "Wait for the locale decision", "--for-person"]) == 0
    kicks.clear()
    assert cli_exit(main, ["orch", "unpark", "1"]) == 0
    assert "waiting-person" not in case.tracker.get_issue(1).labels
    assert kicks and set(kicks) == {"owner/repo"}, f"fa orch unpark makes the issue startable: {kicks}"


def test_closing_an_issue_starts_a_pass(git_history, tmp_path, monkeypatch, isolated_home):  # noqa: F811
    from test_orch_close_superseded import close, superseded_case

    case = superseded_case(git_history, tmp_path, monkeypatch)
    kicks = record_kicks(monkeypatch)
    assert close(case) == 0
    assert case.tracker.get_issue(1).state == "closed"
    assert kicks and set(kicks) == {"owner/repo"}, f"fa orch close frees its files and blockers: {kicks}"


# --- A run that frees a test slot wakes the gate while it still runs -----------------------


def test_a_run_that_frees_a_test_slot_starts_a_pass_in_which_the_queued_issue_starts_on_it(
    git_history, tmp_path, monkeypatch,
):
    """The run frees the machine's only test slot, a pass starts at once, and in that pass
    the queued issue starts while the slot is free for it.

    Every slot acquisition and release is recorded. A kick sent while the run still runs,
    after it released the slot, is the launched gate job: it runs one real gate pass
    (`scheduler.tick`, the body of `fa gate tick`) on its own thread, as a separate
    process would. Its launcher records each issue the pass starts and whether that
    issue's own process could take the test slot at that moment. A kick that finds the
    slot still held, or that comes only after the run finished (the finished-run kick that
    already exists), starts no simulated pass: the five-minute timer is not simulated.
    The queued issue is in another lane, so only the gate's start decision stands between
    it and the freed slot.
    """
    from contextlib import contextmanager
    from dataclasses import replace
    import os
    import time

    from foragent import capacity, machine_budget, runner, scheduler
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    machine = machine_budget.machine_file()
    machine.parent.mkdir(parents=True, exist_ok=True)
    machine.write_text("test_slots: 1\n", encoding="utf-8")

    lanes = {"api": {"paths": ["api/**"], "agent": "fa-api"},
             "view": {"paths": ["view/**"], "agent": "fa-view"}}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, skill_libraries=[])
    case.ctx.config = replace(case.ctx.config, lanes=lanes)
    case.tracker.add_labels(1, ["lane:api"])
    body = case.tracker.get_issue(1).body
    case.tracker.add_issue(2, "Queued task", body, ["ready", "lane:view"])
    case.tracker.record_label(2, "approved", "human")  # An approver's label: a pass can start it.
    bind_cli(case, monkeypatch)  # Writes the project config the kick resolves.
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    events = []
    passes = []
    hold = capacity.hold_test_slot

    @contextmanager
    def watched(*args, **kwargs):
        with hold(*args, **kwargs):
            events.append(("acquired",))
            try:
                yield
            finally:
                events.append(("released",))

    def queued_issue_can_take_a_slot():
        # The queued issue's own process does not inherit this run's held marker.
        saved = os.environ.pop(capacity.TEST_SLOT_ENV, None)
        try:
            with hold(case.ctx.config.test_slots, deadline=time.monotonic()):
                return True
        except TimeoutError:
            return False
        finally:
            if saved is not None:
                os.environ[capacity.TEST_SLOT_ENV] = saved

    def start(n):
        events.append(("started", n, case.store.get_run(1).status, queued_issue_can_take_a_slot()))
        return 7000 + n

    def gate_pass():
        ctx = tick_context(case)
        ctx.launcher = start
        try:
            scheduler.tick(ctx)
        except BaseException as exc:  # Reported below: a thread's error would otherwise be lost.
            events.append(("pass-error", repr(exc)))

    def launched(repo):
        status, free = case.store.get_run(1).status, queued_issue_can_take_a_slot()
        events.append(("kick", status, free))
        if status == "running" and free and ("released",) in events:
            thread = threading.Thread(target=gate_pass)
            thread.start()
            passes.append(thread)
            thread.join(30)  # A separate process: the run is not made to wait for it.

    monkeypatch.setattr(capacity, "hold_test_slot", watched)
    record_kicks(monkeypatch, launched)

    # Harness control: while the run holds the only slot, the queued issue cannot take one.
    with hold(1):
        assert not queued_issue_can_take_a_slot()
    assert queued_issue_can_take_a_slot()

    assert runner.run_issue(1, case.ctx) == "merged"
    for thread in passes:
        thread.join(60)
        assert not thread.is_alive(), "A started gate pass must finish"
    assert ("acquired",) in events and ("released",) in events, f"the run must use a test slot: {events}"
    assert not [event for event in events if event[0] == "pass-error"], f"A started pass must not fail: {events}"
    started = [event for event in events if event[0] == "started" and event[1] == 2 and event[2] == "running"]
    assert started, (
        "After the run frees its test slot it must start a pass at once, while it still runs, and in "
        f"that pass the queued issue must start; events: {events}"
    )
    assert started[0][3], f"The queued issue must start when the freed slot is free for it: {events}"


# --- fa gate status shows this hour's GitHub calls ---------------------------------------


def test_gate_status_shows_github_calls_in_the_current_hour(tick_case, capsys):  # noqa: F811
    case = tick_case
    now = case.clock[0].replace(minute=30, second=0, microsecond=0)
    case.clock[0] = now
    for _ in range(58):
        case.store.count_github_call(now - timedelta(hours=1))
    for _ in range(37):
        case.store.count_github_call(now - timedelta(minutes=10))
    capsys.readouterr()
    assert cli_exit(case.main, ["gate", "status", "--repo-root", str(case.root)]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if "github" in line.lower()]
    shown = [line for line in lines if re.search(r"(?<!\d)37(?!\d)", line)]
    assert shown, f"fa gate status must show the 37 GitHub calls of the current hour: {lines}"
    for line in shown:
        for other in ("58", "95", "74"):
            assert not re.search(rf"(?<!\d){other}(?!\d)", line), (
                f"The count is this hour's calls only, once: {line!r}"
            )
