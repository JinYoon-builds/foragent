"""Shared world for the queue-only-main acceptance tests (helper, not a test file).

One merge-owner project with a real local Git origin (the offline transport from
tests/test_order.py imports commits and pushes instead of reaching a network),
the fake GitHub tracker and the fake measured test runs of the publication queue
(tests/test_order_publications.py). Nothing here reaches a network, a model or a
real signal. Every network-shaped call (fetch, push, ls-remote, any tracker call)
goes through seams this module can observe or slow down.
"""

from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from conftest import git_command
from foragent import merge_owner, order, order_flush, order_notes, order_prepare, order_prepare_check
from foragent import order_prepare_cycle, order_prepare_setup, order_publications
from foragent.git import GitRepo
from foragent.scheduler import TickReport
from foragent.state import StateStore

BOUNDARY = "after-train"
PASS_OUTPUT = "1 passed in 0.01s: the command already passes on current main"
NETWORK_VERBS = {"fetch", "push", "ls-remote"}


class Stopped(BaseException):
    """The owner process stops here (an injected crash, not an error it can handle)."""


def verb(args):
    args = list(args)
    while args[:1] == ["-C"]:
        args = args[2:]
    while args[:1] == ["-c"]:
        args = args[2:]
    return args[0] if args else ""


def make_plan(identity):
    test = f"tests/acceptance/test_{identity.replace('-', '_')}.py"
    return {"id": identity, "title": f"Order {identity}", "summary": "Goal",
            "spec_markdown": f"# Order {identity}\n",
            "issues": [{"key": "new", "title": f"{identity}: behaviour", "goal": "Add observable behaviour",
                        "out_of_scope": "Other behaviour", "done_when": [f"uv run pytest {test}"], "lane": "main",
                        "size": "s", "statements": ["The new behaviour works"],
                        "acceptance_files": {test: "def test_new():\n    assert False\n"}}]}


def locked_file(plan):
    return next(iter(plan["issues"][0]["acceptance_files"]))


def command(plan):
    return plan["issues"][0]["done_when"][0]


class World:
    """Accessors over the shared state; every read is of origin main, the tracker or the queue."""

    def __init__(self, case, root, git, calls, origin):
        self.case, self.root, self.git, self.calls, self.origin = case, root, git, calls, origin
        self.config = case.config
        self.tracker = case.tracker
        self.events = case.events
        self.passing = set()       # Done when commands that already pass on main.
        self.measured = []         # (command, base) for every red-check measurement.
        self.reports = []

    @property
    def store(self):
        return self.case.ctx.store

    @property
    def ctx(self):
        return self.case.ctx

    # --- orders -------------------------------------------------------------------------
    def prepared(self, plan):
        """Record a fresh, reviewed preparation of ``plan`` on current main, as fa order prepare does."""
        files = order.validate_plan(plan, self.root, self.config)
        item = plan["issues"][0]
        entry = {"key": "new", "status": "reviewed", "phase": "reviewing_tests", "reason": "Red",
                 "written": item["acceptance_files"], "done_when": list(item["done_when"]),
                 "base_revision": self.main(),
                 "cycle": order_prepare_cycle.identity(plan, item, self.config, self.root, files),
                 "published": order_prepare_setup.published_digest(plan, self.root, self.config, files),
                 "others": {}, "attempts": []}
        path = order_prepare._record_path(self.store, plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        order_prepare._write(path, {"plan": plan["id"], "issues": {"new": entry}})
        return plan

    def publish(self, plan, **options):
        """`fa order publish PLAN --approve`: with merge_owner on this is a request to the owner."""
        return order.publish(plan, self.root, self.config, self.tracker, repo=self.config.repo,
                             git=self.git, **({"approve": True} | options))

    def defer(self, plan, boundary=BOUNDARY):
        """`fa order publish PLAN --approve --defer BOUNDARY`."""
        return self.publish(plan, defer=boundary)

    def note(self, text, boundary=BOUNDARY, kind="decision"):
        """`fa order note --defer BOUNDARY --kind decision --text TEXT`."""
        order_notes.append(self.store, kind=kind, boundary=boundary, content=text)

    def flush(self, boundary=BOUNDARY):
        """`fa order flush --boundary BOUNDARY` (the command's body, with this world's adapters)."""
        return order_flush.flush(self.root, self.config, self.tracker, self.git, self.store,
                                 repo=self.config.repo, boundary=boundary)

    def owner_pass(self):
        """One gate pass: the off-owner red checker, then one merge-owner pass."""
        report = TickReport()
        order_publications.check_pending(self.ctx, report)
        merge_owner.tick(self.ctx, report)
        self.reports.append(report)
        return report

    def restart(self):
        """A new process over the same durable folder."""
        self.case.store = self.case.ctx.store = StateStore(self.store.root)

    # --- observations -------------------------------------------------------------------
    def main(self):
        return git_command(self.origin, "rev-parse", "main")

    def commits_since(self, before):
        return git_command(self.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()

    def on_main(self, name):
        names = git_command(self.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
        return git_command(self.origin, "show", f"main:{name}") if name in names else None

    def pushes(self):
        return sum(1 for args in self.calls if verb(args) == "push")

    def issues_of(self, plan):
        """{title: [issues]} for the order's parent and child titles (every copy, to see duplicates)."""
        titles = {plan["title"], plan["issues"][0]["title"]}
        found = {title: [] for title in titles}
        for issue in self.tracker.list_issues():
            if issue.title in titles:
                found[issue.title].append(issue)
        return found

    def child(self, plan):
        marker = order._child_marker(plan["id"], "new")
        return [issue for issue in self.tracker.list_issues() if marker in issue.body]

    def requests_of(self, plan):
        return [row for row in order_publications.requests(self.store) if row["plan"]["id"] == plan["id"]]

    def lock_timeouts(self):
        events = []
        for path in self.store.root.rglob("orch-events.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line).get("kind") == "lock-timeout":
                    events.append(json.loads(line))
        return events


@pytest.fixture
def world(case, offline_publication, monkeypatch):
    """The publication case on a real local origin, with per-command measured red checks."""
    root, _config, _tracker, git, calls, origin = offline_publication
    case.root = case.ctx.repo_root = root
    case.git = case.ctx.git = git
    case.config = case.ctx.config = replace(case.config, merge_batch=True)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "5")
    box = World(case, root, git, calls, origin)

    def measured(git, base, context, place, verify, commands, directory, timeout_s, *, collect_node_ids=False):
        runs = []
        for line in commands:
            names = [part for part in line.split() if part.endswith(".py")]
            nodes = [f"{name}::test_new" for name in names]
            passing = line in box.passing
            box.measured.append((line, base))
            box.events.append(("red-check", line))
            runs.append({"command": line, "exit_code": 0 if passing else 1, "timed_out": False,
                         "output": PASS_OUTPUT if passing else "1 failed: still red on current main",
                         "changed": [], "failing_node_ids": [] if passing else nodes,
                         "collected_node_ids": nodes, "nonred_node_ids": nodes if passing else []})
        return runs

    monkeypatch.setattr(order_prepare_check, "run", measured)
    # No merge candidates in these worlds: the owner's PR admissions are empty.
    monkeypatch.setattr(merge_owner, "admissions", lambda store: {})
    return box


# --- the merge train -------------------------------------------------------------------------

class Train:
    """A merge train on origin: its ref exists while CI runs; it lands when CI passes.

    The owner's train step (``merge_owner_batch.prepare``) is the seam: a pass with CI
    pending keeps the train; the first pass after CI passes lands it on main and removes
    the ref, as the owner's batch landing does.
    """

    REF = "refs/heads/foragent/batch-merge-0001"
    FILE = "src/train_member.py"

    def __init__(self, box, monkeypatch, *, ci_passes_at_pass):
        self.box = box
        self.ci_passes_at_pass = ci_passes_at_pass
        self.landed_commit = None
        self.steps = 0
        git_command(box.origin, "update-ref", self.REF, box.main())
        from foragent import merge_owner_batch

        monkeypatch.setattr(merge_owner_batch, "prepare", self.step)
        monkeypatch.setattr(merge_owner_batch, "retire", lambda ctx, report: self.step(ctx, report, []))

    def step(self, ctx, report, candidates):
        self.steps += 1
        if self.landed_commit is not None:
            return None
        if self.steps < self.ci_passes_at_pass:
            report.actions.append("train CI pending")
            return False
        tip = self.box.main()
        message = "Merge train 0001"
        stream = ("commit refs/heads/main\n"
                  "committer Offline Tests <tests@example.com> 1792483200 +0000\n"
                  f"data {len(message)}\n{message}\n"
                  f"from {tip}\n"
                  f"M 100644 inline {self.FILE}\ndata 12\nVALUE = 'x'\n\n"
                  "done\n")
        git_command(self.box.origin, "fast-import", "--quiet", input=stream)
        git_command(self.box.origin, "update-ref", "-d", self.REF)
        self.landed_commit = self.box.main()
        report.actions.append("train landed")
        return False

    @property
    def running(self):
        return bool(git_command(self.box.origin, "for-each-ref", self.REF).strip())


# --- network observation -------------------------------------------------------------------

def _fd_path(fd):
    try:
        if sys.platform == "darwin":
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0"))
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


class Guard:
    """Fails nothing by itself: records each fake network call made while a Foragent lock is held."""

    def __init__(self):
        self.real_flock = fcntl.flock
        self.held = {}
        self.calls = []
        self.violations = []
        self.mutex = threading.RLock()
        self.active = True

    def flock(self, fd, operation):
        self.real_flock(fd, operation)
        number = fd if isinstance(fd, int) else fd.fileno()
        caller = sys._getframe(1).f_globals.get("__name__", "")
        with self.mutex:
            if operation & fcntl.LOCK_UN:
                self.held.pop(number, None)
            elif caller == "foragent" or caller.startswith("foragent."):
                path = _fd_path(number)
                if path is not None:
                    self.held[number] = path

    def holding(self):
        """Locks still held now, proven by a nonblocking probe from a separate descriptor."""
        held = []
        with self.mutex:
            for number, path in list(self.held.items()):
                try:
                    probe = os.open(path, os.O_RDONLY)
                except OSError:
                    self.held.pop(number, None)
                    continue
                try:
                    self.real_flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(Path(path).name)
                else:
                    self.real_flock(probe, fcntl.LOCK_UN)
                    self.held.pop(number, None)
                finally:
                    os.close(probe)
        return sorted(held)

    def network(self, what):
        if not self.active:
            return
        self.calls.append(what)
        held = self.holding()
        if held:
            self.violations.append(f"{what} while holding {', '.join(held)}")


@contextmanager
def guarded(box, monkeypatch):
    guard = Guard()
    monkeypatch.setattr(fcntl, "flock", guard.flock)
    real_call = GitRepo._call

    def call(self, args):
        if verb(args) in NETWORK_VERBS:
            guard.network(f"git {verb(args)}")
        return real_call(self, args)

    monkeypatch.setattr(GitRepo, "_call", call)
    tracker = box.tracker
    for name in dir(type(tracker)):
        if name.startswith("_") or not callable(getattr(tracker, name, None)):
            continue
        original = getattr(tracker, name)

        def wrapped(*args, __name=name, __original=original, **kwargs):
            guard.network(f"GitHub {__name}")
            return __original(*args, **kwargs)

        monkeypatch.setattr(tracker, name, wrapped)
    try:
        yield guard
    finally:
        guard.active = False


@contextmanager
def slow_push(monkeypatch, *, seconds=30.0):
    """Every push waits until released (at most ``seconds``); ``pushing`` is set while one waits."""
    pushing, release = threading.Event(), threading.Event()
    real_call = GitRepo._call

    def call(self, args):
        if verb(args) == "push":
            pushing.set()
            release.wait(seconds)
        return real_call(self, args)

    monkeypatch.setattr(GitRepo, "_call", call)
    try:
        yield pushing, release
    finally:
        release.set()


@contextmanager
def stop_after(monkeypatch, step):
    """The process stops right after its first ``commit`` (any commit object) or first ``push``."""
    real_call = GitRepo._call
    fired = []

    def call(self, args):
        result = real_call(self, args)
        name = verb(args)
        if not fired and (name == "push" if step == "push" else name in {"commit", "commit-tree"}):
            fired.append(name)
            raise Stopped(f"owner stopped after {name}")
        return result

    monkeypatch.setattr(GitRepo, "_call", call)
    try:
        yield fired
    finally:
        monkeypatch.setattr(GitRepo, "_call", real_call)
