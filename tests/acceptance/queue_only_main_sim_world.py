"""Simulation world for S2 (queue-only-main): the unattended-cycle world with a merge train.

Helpers, not tests. Built on tests/acceptance/unattended_cycle_world.py (the S1 world): the
real scheduler, runner, GhTracker, publication check and merge owner run against local Git,
the executable fake GitHub service and fake model executors. This module adds only:

- batch merge on (``merge_batch: true``) in the committed project settings, so two
  merge-queued issues become one real merge train built by the merge owner;
- a held train CI: a push of a ``foragent/batch-merge-*`` branch registers no CI run until
  the test releases it, so the train's CI stays pending for as long as the test needs;
- a generic fake worker that writes ``src/member_N.py`` for the member its issue names.

No network, model, person or signal is reached; the S1 world's guards stay on.
"""

import json
import multiprocessing
from pathlib import Path
import re
import subprocess
import threading
import time

from conftest import git_command
from test_merge_owner_e2e_sandbox import Sandbox, service_file
from unattended_cycle_world import PLAIN_LOCK, World
from foragent import runner
from foragent.config import load_config
from foragent.executors import ExecResult
from foragent.state import StateStore
from foragent.tracker import GhTracker

TRAIN_PREFIX = "foragent/batch-merge-"
MEMBER = re.compile(r"src/member_(\d+)\.py")


def member_order(world, identity, numbers):
    """An approved order with one new issue per member number; each issue's test needs its member."""
    issues = []
    for n in numbers:
        test = f"tests/acceptance/test_member_{n}.py"
        issues.append({
            "key": f"m{n}", "title": f"Member {n}", "lane": "main", "size": "s",
            "goal": f"Deliver member {n}.", "out_of_scope": "Other members.",
            "files": [f"src/member_{n}.py"],
            "done_when": [world.config.test_command + f" {test}"],
            "acceptance_files": {test: ("import runpy\n\n\n"
                                        f"def test_member_{n}():\n"
                                        f"    assert runpy.run_path('src/member_{n}.py')['VALUE'] == {n}\n")}})
    return {"id": identity, "title": f"Order: {identity}", "summary": f"Add members {numbers}.",
            "spec_markdown": f"# {identity}\n\nAdd members {numbers}.\n", "issues": issues}


class TrainWorld(World):
    """The S1 world with no seeded issue, batch merge on and a train CI the test controls."""

    def __init__(self, root, monkeypatch):
        super().__init__(root, monkeypatch, lock=PLAIN_LOCK, seed_issue=False)
        settings = json.loads(git_command(self.origin, "show", "main:.foragent.yml"))
        settings.update(merge_batch=True, merge_batch_size=2)
        self.main_update({".foragent.yml": json.dumps(settings)}, "Batch merge on")
        git_command(self.repo, "pull", "-q", "--ff-only", "origin", "main")
        self.config = load_config(self.repo)
        assert self.config.merge_batch and self.config.merge_owner
        self.hold_train_ci = True
        self.held = []          # (branch, head) of train pushes whose CI is not registered yet.
        self.orders = {}

    # --- the train CI ---------------------------------------------------------------------
    def git_transport(self, argv):
        args = list(argv)
        while args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["push"] and args[-1].startswith(":"):
            # A ref deletion (`git push origin :refs/heads/B`), which the S1 transport does not
            # emulate: honour the lease, then delete the ref on the origin, as GitHub would.
            branch = args[-1][1:].removeprefix("refs/heads/")
            current = git_command(self.origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")
            for arg in args:
                if arg.startswith("--force-with-lease="):
                    assert current.strip() == arg.rsplit(":", 1)[1], (branch, current, arg)
            git_command(self.origin, "update-ref", "-d", f"refs/heads/{branch}")
            return ""
        if args[:1] == ["push"] and self.hold_train_ci:
            destination = args[-1].split(":", 1)[-1].removeprefix("refs/heads/")
            if destination.startswith(TRAIN_PREFIX):
                self.publish_measurement = lambda root, head: self.held.append((destination, head))
                try:
                    return super().git_transport(argv)
                finally:
                    del self.publish_measurement
        return super().git_transport(argv)

    def train_refs(self):
        out = git_command(self.origin, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{TRAIN_PREFIX}*")
        return [line for line in out.splitlines() if line.strip()]

    def train_pending(self):
        return bool(self.train_refs()) and bool(self.held)

    def release_train_ci(self):
        """The train's CI finishes: its run and acceptance report become visible to the owner."""
        self.hold_train_ci = False
        for branch, head in self.held:
            git_command(self.repo, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            Sandbox.publish_measurement(self, self.repo, head)
            with service_file(self.root) as state:
                # GitHub names a run's branch; the owner's train cleanup reads it.
                state["ci"][head]["run"]["head_branch"] = branch
        self.held = []

    # --- workers ----------------------------------------------------------------------------
    def launch_worker(self, n):
        def work():
            self.assert_isolated()

            class Model:
                def __init__(self, name):
                    self.name = name

                def run(executor_self, **kwargs):
                    with (self.root / "executor-turns.jsonl").open("a") as stream:
                        stream.write(json.dumps({"issue": n, "executor": executor_self.name}) + "\n")
                    if executor_self.name == "claude":
                        found = MEMBER.search(kwargs.get("prompt", ""))
                        assert found, "the fake worker needs the member its issue names"
                        number = found.group(1)
                        (kwargs["worktree"] / f"src/member_{number}.py").write_text(f"VALUE = {number}\n")
                    return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

            from foragent.git import GitRepo

            ctx = runner.RunContext("sandbox/repo", self.repo, GhTracker("sandbox/repo"),
                StateStore(self.state_root, now=lambda: self.clock), self.config,
                GitRepo(self.repo, self.git_transport), now=lambda: self.clock,
                executor_factory=Model,
                sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("Worker polled")),
                poll_hook=lambda execute, poll: execute())
            outcome = runner.run_issue(n, ctx)
            (self.root / f"worker-{n}-{time.monotonic_ns()}.json").write_text(json.dumps({"outcome": outcome}))

        process = multiprocessing.get_context("fork").Process(target=work)
        process.start()
        self.workers.append(process)
        process.join(120)
        assert not process.is_alive(), "Sandbox worker did not exit naturally; no signals are allowed"
        assert process.exitcode == 0, StateStore(self.state_root).log_path(n).read_text()
        return None

    # --- orders through fa commands -------------------------------------------------------------
    def write_order(self, plan):
        folder = self.root / "orders"
        folder.mkdir(exist_ok=True)
        path = folder / f"{plan['id']}.json"
        path.write_text(json.dumps(plan, indent=2))
        self.orders[plan["id"]] = plan
        code, out = self.fa("order", "preread", str(path))
        assert code == 0, out
        return path

    # --- observations -----------------------------------------------------------------------
    def issue_numbers(self, titles):
        state = self.state()
        return {issue["title"]: int(n) for n, issue in state["issues"].items() if issue["title"] in titles}

    def first_parent(self):
        return git_command(self.origin, "rev-list", "--first-parent", "--reverse", "main").splitlines()

    def introduced(self, path):
        """The first first-parent main commit that has ``path``, or None."""
        for sha in self.first_parent():
            names = git_command(self.origin, "ls-tree", "-r", "--name-only", sha).splitlines()
            if path in names:
                return sha
        return None

    def lock_timeouts(self):
        events = []
        for path in Path(self.state_root).rglob("orch-events.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line).get("kind") == "lock-timeout":
                    events.append(json.loads(line))
        return events


class ClaimDuringCommand:
    """Make one preparation claim while a command run in this thread is in its first GitHub call.

    The claim runs in another thread with the public ``order_prepare_owner.claim``; the command
    waits for it (at most ``grace`` seconds) before its GitHub call continues. Nothing is
    replaced: the command, the claim and every lock are real.
    """

    def __init__(self, world, monkeypatch, *, grace=2.0):
        self.world, self.grace = world, grace
        self.armed = False
        self.during_train = None
        self.result, self.elapsed, self.error = None, None, None
        self.done = threading.Event()
        self.started = False
        popen = subprocess.Popen
        owner = threading.current_thread()

        def watched(argv, *args, **kwargs):
            if (self.armed and threading.current_thread() is owner and isinstance(argv, (list, tuple))
                    and Path(argv[0]).name == "gh"):
                self.armed = False
                self.start()
                self.done.wait(self.grace)
            return popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", watched)

    def start(self):
        from foragent import order_prepare_owner

        self.started = True
        self.during_train = self.world.train_pending()
        folder = Path(self.world.state_root) / "prepare"
        record = {"plan": "unrelated-order", "issues": {}}

        def run():
            began = time.monotonic()
            try:
                self.result = order_prepare_owner.claim(folder, record, ["unrelated"], lambda: None)
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                self.error = repr(exc)
            finally:
                self.elapsed = time.monotonic() - began
                self.done.set()

        threading.Thread(target=run, daemon=True).start()

    def finish(self, timeout=30):
        if not self.started:
            self.start()
        self.done.wait(timeout)


