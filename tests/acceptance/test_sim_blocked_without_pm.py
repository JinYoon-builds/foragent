"""Acceptance test for sim-blocked-without-pm (simulation test S3).

Statement: in the simulation world, an issue held by a plumbing timeout (a baseline wait cut)
reaches merged after the orchestrator retries it with fa commands only, with no PM or person
action.

The world is the S1 unattended-cycle world (#2452): the real scheduler, runner, tracker,
publication check and merge owner against local Git, a fake GitHub service and fake model
executors. The scenario replays the 11-07 incident (#2448, architect r1018):

1. Issue #1's change also edits the acceptance workflow, so the PR head's workflow differs
   from main and its CI artifacts cannot be trusted. (Since behind-workflow-no-local-baseline, a
   head that only lacks a later workflow edit on main is updated instead; a head that edits the
   workflow itself still falls back to the local baseline, which is the wait this test cuts.)
2. The run falls back to a local acceptance baseline. Another run is already building that same
   baseline and holds its lock, so the run waits; the wait is cut at the lock limit (here 2 s
   through FORAGENT_LOCK_TIMEOUT_SECONDS instead of 600 s).
3. The issue is held (blocked) and reaches the orchestrator.
4. The other build ends. A scripted orchestrator that acts only through fa commands retries the
   issue; the retry builds on current main and the issue reaches merged.

No PM question, PM inbox call or needs-human hand-off may happen. A runner crash is not a hold:
the cut wait must end the run in a state the orchestrator sees as blocked.
"""

import fcntl
import json
import re
import subprocess
import sys

from test_merge_owner_e2e_sandbox import WORKFLOW
from unattended_cycle_world import MEMBER, PLAIN_LOCK, FakeOrchestrator, World
from foragent import runner
from foragent.state import StateStore

LOCK_LIMIT_SECONDS = "2"
WORKFLOW_EDIT = "\n# acceptance workflow edited by issue 1\n"
BASELINE_KEY = re.compile(r"[0-9a-f]{64}")


class S3World(World):
    """The S1 world; a worker that ends with an error is recorded, not raised inside a tick."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.crashed = []

    def launch_worker(self, n):
        try:
            return super().launch_worker(n)
        except AssertionError as exc:
            if "Sandbox worker did not exit naturally" in str(exc):
                raise
            self.crashed.append({"issue": n, "log_tail": str(exc)[-1500:]})
            return None

    def tick(self, **kwargs):
        try:
            return super().tick(**kwargs)
        except AssertionError:
            if self.crashed:
                return None  # A crashed worker leaves its run open; the test reports the crash itself.
            raise


class OtherBaselineBuild:
    """Another run that started building the same acceptance baseline just before this run.

    The first time any run asks for an acceptance-baseline cache key, this fake build takes the
    key's lock through its own open file (a separate holder) and keeps it while that run waits.
    The hold ends when the holding process exits or when ``finish`` is called.
    """

    def __init__(self, world, monkeypatch):
        self.marker = world.root / "other-baseline-build.json"
        self.held = []
        real = StateStore.lock_acceptance_baseline
        other = self

        def lock_acceptance_baseline(store, key, *args, **kwargs):
            if (store.root.name == "baselines" and BASELINE_KEY.fullmatch(key)
                    and not other.marker.exists()):
                store.root.mkdir(parents=True, exist_ok=True)
                holder = (store.root / f"{key}.lock").open("a", encoding="utf-8")
                fcntl.flock(holder, fcntl.LOCK_EX)
                other.held.append(holder)
                other.marker.write_text(json.dumps({"key": key}))
            return real(store, key, *args, **kwargs)

        monkeypatch.setattr(StateStore, "lock_acceptance_baseline", lock_acceptance_baseline)

    @property
    def started(self):
        return self.marker.exists()

    @property
    def key(self):
        return json.loads(self.marker.read_text())["key"]

    def finish(self):
        for holder in self.held:
            holder.close()
        self.held.clear()


def issue_edits_workflow(monkeypatch):
    """The fake implementer of issue #1 also edits the acceptance workflow in its worktree."""
    real_run = runner.run_issue

    def run_issue(n, ctx, *args, **kwargs):
        factory = ctx.executor_factory

        def executor(name):
            model = factory(name)
            real = model.run

            def run(**kwargs):
                result = real(**kwargs)
                path = kwargs["worktree"] / WORKFLOW
                if name == "claude" and n == 1 and WORKFLOW_EDIT not in path.read_text():
                    path.write_text(path.read_text() + WORKFLOW_EDIT)
                return result

            model.run = run
            return model

        ctx.executor_factory = executor
        return real_run(n, ctx, *args, **kwargs)

    monkeypatch.setattr(runner, "run_issue", run_issue)


def fake_process_table(monkeypatch):
    """Lock-timeout diagnostics list lock holders; this world's process table is empty.

    The sandbox refuses every real system tool; ``lsof`` becomes a process that prints nothing.
    No signal is ever sent.
    """
    popen = subprocess.Popen

    def guarded(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]).rsplit("/", 1)[-1] == "lsof":
            argv = [sys.executable, "-c", ""]
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)


def assert_no_pm_or_person(world, orchestrator):
    assert world.pm_questions() == [], "No question was recorded for the PM session or a person"
    assert world.pm_calls == [], "Nothing asked or answered through the PM inbox"
    assert world.person_routed() == set(), "No issue was handed to a person (needs-human)"
    assert orchestrator.questions() == []


def test_issue_held_by_a_baseline_wait_cut_reaches_merged_after_the_orchestrator_retries_it_with_fa_commands_only(
        tmp_path, monkeypatch):
    world = S3World(tmp_path, monkeypatch, lock=PLAIN_LOCK)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", LOCK_LIMIT_SECONDS)
    fake_process_table(monkeypatch)
    other = OtherBaselineBuild(world, monkeypatch)
    issue_edits_workflow(monkeypatch)
    orchestrator = FakeOrchestrator(world)

    # 1-3: the run's local baseline wait is cut and the issue is held.
    assert world.drive(lambda w: "blocked" in w.labels(1) or w.merged() or w.crashed, ticks=10, step=300), (
        "The issue must end its run: " + world.store().log_path(1).read_text()[-2000:])
    assert world.crashed == [], (
        "A cut baseline wait must end the run as a hold, not as a runner crash: " + json.dumps(world.crashed)[-1500:])
    assert other.started, "The run must have waited for the acceptance baseline another run was building"
    assert world.merged() == [], "A run whose baseline wait was cut must not merge"
    assert "blocked" in world.labels(1), "The issue must be held by the cut wait"
    blocked = orchestrator.blocked()
    assert 1 in blocked, "The held issue must be listed for the orchestrator"
    details = blocked[1]["details"] + blocked[1]["log_tail"]
    assert other.key in details and "Timed out waiting" in details, (
        "The blocker must name the baseline wait that was cut: " + details[-1500:])
    assert_no_pm_or_person(world, orchestrator)

    # 4: the other build ends; the orchestrator retries with fa commands only.
    other.finish()
    orchestrator.resume(1)
    assert world.drive(lambda w: w.merged(), ticks=15, step=300), (
        "After the orchestrator's retry the same issue must reach merged: "
        + world.store().log_path(1).read_text()[-2000:])

    state = world.state()
    assert [row["subject"] for row in state["merges"]] == ["Member 1 (#1)"], "Merged exactly once"
    assert state["issues"]["1"]["state"] == "closed" and state["closures"].count(1) == 1
    assert world.main_file(MEMBER) == "VALUE = 1\nHANDLED = 2"
    assert WORKFLOW_EDIT.strip() in world.main_file(WORKFLOW), "The issue's workflow edit lands with it"
    retries = [cmd for cmd in orchestrator.commands if cmd[:2] == ["orch", "retry"]]
    assert retries == [["orch", "retry", "1"]], "One retry of the held issue, through fa"
    assert {tuple(cmd[:2]) for cmd in orchestrator.commands} <= {("orch", "list"), ("orch", "questions"),
                                                                ("orch", "retry")}
    assert "blocked" not in world.labels(1) and "needs-human" not in world.labels(1)
    assert world.network_calls == [], "No network call"
    assert_no_pm_or_person(world, orchestrator)
