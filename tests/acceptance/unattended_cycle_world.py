"""One sandbox project for the unattended-cycle scenario (helpers, not tests).

Built on the merge-owner sandbox (tests/test_merge_owner_e2e_sandbox.py): the real scheduler,
runner, tracker, publication check and merge owner run against local Git, an executable fake
GitHub service and fake model executors. This world adds only what the scenario needs:

- one approved issue (#1) whose locked test can go stale when main changes under it;
- fake GitHub routes for editing an issue body and recording who labelled it;
- a scripted fake orchestrator that acts only through ``fa`` commands (``foragent.cli.main``).

Every call to the PM session, a person or the network is recorded or refused, so a test can
assert none happened.
"""

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys

from conftest import git_command
from test_merge_owner_e2e_sandbox import FAKE_GH, Sandbox, service_file
from foragent import cli, main_write_requests, merge_owner, orch_inbox, order, order_publications
from foragent import order_publications_process, runner
from foragent.scheduler import TickReport
from foragent.executors import ExecResult
from foragent.order_files import issue_body
from foragent.run_previous import retry_marker
from foragent.state import StateStore
from foragent.tracker import GhTracker

LOCKED = "tests/acceptance/test_own_1.py"
CASES = "src/cases.py"
MEMBER = "src/member_1.py"
TEST_COMMAND_SUFFIX = f" {LOCKED}"

# The locked test of issue #1. Its purpose: member_1 exists and handles every known case.
# The second test only assumes how many cases main has (two). When merged main adds a third
# case it goes stale: it fails on every branch, while its purpose is unchanged.
_LOCK = (
    "from pathlib import Path\nimport runpy\n\n\n"
    "def test_member_handles_every_case():\n"
    "    cases = runpy.run_path('src/cases.py')['CASES']\n"
    "    assert Path('src/member_1.py').exists()\n"
    "    assert runpy.run_path('src/member_1.py')['HANDLED'] == len(cases)\n\n\n"
    "def test_known_cases():\n"
    "    assert len(runpy.run_path('src/cases.py')['CASES']) == {count}\n")
STALE_LOCK = _LOCK.format(count=2)
# The lock-only correction: the same purpose, the count merged main changed. It passes on
# main by nature (main has three cases), so only the issue branch can prove it.
CORRECTED_LOCK = _LOCK.format(count=3)
# A lock that stays true when main moves: the plain unattended path.
PLAIN_LOCK = _LOCK.replace("== {count}", ">= 2").format()

# Issue #1 as its order published it (Done when is added once the test command is known).
ISSUE_ONE = {"goal": "Deliver member 1: it handles every known case.", "out_of_scope": "Other members.",
             "files": [MEMBER]}

# Extra fake GitHub routes: an issue body edit, label events with the account's login,
# issue creation, and label creation. Anything else still fails as unknown argv.
EXTRA_GH = r'''
import fcntl, json, os, pathlib, sys
_root = pathlib.Path(os.environ['FA_SANDBOX']).resolve()
_args = sys.argv[1:]
def _extra():
    path = _root / 'github.json'
    with (_root / 'github.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text())
        handled, result = False, None
        method = _args[_args.index('-X') + 1] if '-X' in _args else 'GET'
        endpoint = next((a for a in _args[1:] if a.startswith('repos/')), '') if _args[:1] == ['api'] else ''
        route = endpoint.split('?')[0].removeprefix('repos/sandbox/repo').strip('/').split('/')
        if _args[:2] == ['api', 'user']:
            handled, result = True, {'login': 'human'}
            if '--jq' in _args:
                handled, result = True, None
                print('human')
        elif _args[:1] == ['api'] and route[:1] == ['issues'] and len(route) == 2 and method == 'PATCH':
            issue = state['issues'][route[1]]
            fields = {}
            data = sys.stdin.read()
            if data.strip():
                fields.update(json.loads(data))
            for arg in _args:
                if '=' in arg and not arg.startswith('repos/'):
                    key, value = arg.split('=', 1)
                    if key in ('title', 'body', 'state'):
                        fields[key] = value
            issue.update({k: v for k, v in fields.items() if k in ('title', 'body', 'state')})
            state['edits'].append({'issue': int(route[1]), 'fields': sorted(fields)})
            handled, result = True, issue
        elif (_args[:1] == ['api'] and route[:1] == ['issues'] and len(route) == 3 and route[2] == 'labels'
              and method == 'POST'):
            for arg in _args:
                if arg.startswith('labels[]='):
                    state['events'].setdefault(route[1], []).append(
                        {'label': {'name': arg.split('=', 1)[1]}, 'event': 'labeled',
                         'actor': {'login': 'human'}, 'created_at': state['now']})
        elif _args[:1] == ['api'] and route == ['issues'] and method == 'POST':
            fields = json.loads(sys.stdin.read())
            number = max([int(k) for k in state['issues']] + [0]) + 1
            labels = [{'name': name} for name in sorted(fields.get('labels', []))]
            state['issues'][str(number)] = {'number': number, 'id': number, 'title': fields['title'],
                                            'body': fields.get('body', ''), 'state': 'open', 'labels': labels}
            state['events'][str(number)] = [
                {'label': {'name': label['name']}, 'event': 'labeled', 'actor': {'login': 'human'},
                 'created_at': state['now']} for label in labels]
            state['created'].append(number)
            handled, result = True, state['issues'][str(number)]
        elif (_args[:1] == ['api'] and route[:1] == ['issues'] and len(route) >= 3
              and route[2] in ('sub_issues', 'dependencies') and method in ('POST', 'DELETE')):
            state['links'].append([method, *route[1:]])
            handled, result = True, {}
        elif _args[:1] == ['api'] and route[:1] == ['labels']:
            handled, result = True, ([] if method == 'GET' else {'name': 'label'})
        state['calls_extra'] = state.get('calls_extra', 0) + 1
        path.write_text(json.dumps(state))
    if handled:
        if result is not None:
            print(json.dumps(result))
        sys.exit(0)
_extra()
'''


class World(Sandbox):
    """The merge-owner sandbox reduced to one approved issue (#1)."""

    def __init__(self, root, monkeypatch, *, lock=STALE_LOCK, seed_issue=True):
        super().__init__(root, monkeypatch)
        # fa commands run on the real clock; the gate's clock starts there and only moves forward.
        self.clock = datetime.now(UTC).replace(microsecond=0)
        self.command = self.config.test_command + f" {LOCKED}"
        gh = self.root / "bin" / "gh"
        gh.write_text(f"#!{sys.executable}\n" + EXTRA_GH + FAKE_GH)
        gh.chmod(0o755)
        # Members 2, 4 and 5 are not part of this scenario: their tests pass and their issues are gone.
        seeded = {CASES: "CASES = ['a', 'b']\n", LOCKED: lock,
                  **{f"src/member_{n}.py": f"VALUE = {n}\n" for n in (2, 4, 5)}}
        if not seed_issue:
            seeded[MEMBER] = "VALUE = 1\nHANDLED = 2\n"  # Issue #1's work is already on main.
        self.main_update(seeded, "Scenario seed")
        with service_file(self.root) as state:
            for n in ("2", "4", "5") if seed_issue else ("1", "2", "4", "5"):
                state["issues"].pop(n)
                state["events"].pop(n)
            state.update(edits=[], created=[], links=[], lose_merge_response=False, now=self.clock.isoformat())
            for events in state["events"].values():
                for event in events:
                    event["created_at"] = self.clock.isoformat()
            if seed_issue:
                # Issue #1 in the shape an order publishes, so a lock-only edit changes only its lock.
                state["issues"]["1"].update(id=1, body=issue_body(ISSUE_ONE | {"done_when": [self.command]},
                                                                  "sandbox/repo", "main"))
        git_command(self.repo, "pull", "-q", "--ff-only", "origin", "main")
        self.pm_calls, self.person_calls, self.network_calls = [], [], []
        self.worker_outcomes = []
        self.handled = {}  # Worker attempt counter.
        guard_network(monkeypatch, self.network_calls)
        guard_people(monkeypatch, self.pm_calls, self.person_calls)
        # The detached owner and publication-check launchers are external resources: they run
        # here, in this process, right after the scheduler tick (the owner inside Sandbox.tick).
        self.check_requests = []
        self.reader_turns = []
        monkeypatch.setattr(order, "executor_for", lambda name: FakeReader(name, self.reader_turns))
        monkeypatch.setattr(merge_owner, "start", self._owner_start)
        monkeypatch.setattr(order_publications_process, "start", self._checks_start)
        # A kicked tick is a detached real gate process; here the scenario drives every gate
        # tick itself, so a kick is only recorded (it would otherwise hold the tick lock).
        self.kicks = []
        guard_kicks(monkeypatch, self.kicks)

    def _owner_start(self, ctx, report):
        if (merge_owner.admissions(ctx.store) or main_write_requests.pending(ctx.store)
                or any((ctx.store.root / "merge-owner").glob("*.json"))
                or order_publications.needs_owner(ctx.store)):
            self.owner_requests.append(ctx)

    def _checks_start(self, ctx, report):
        if ctx.config.merge_owner and order_publications.pending(ctx.store):
            self.check_requests.append(ctx)

    def tick(self, *, start_runs=False):
        store = super().tick(start_runs=start_runs)
        while self.check_requests:
            ctx = self.check_requests.pop(0)
            lock = ctx.store.lock_acceptance_baseline(order_publications_process.LOCK, blocking=False)
            assert lock is not None, "Publication checks must run alone"
            with lock:
                order_publications.check_pending(ctx, TickReport())
        return store

    def state(self):
        return json.loads((self.root / "github.json").read_text())

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
                        tree = kwargs["worktree"]
                        if "src/member_7.py" in kwargs.get("prompt", ""):
                            (tree / "src/member_7.py").write_text("VALUE = 7\n")
                            return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])
                        cases = (tree / CASES).read_text()
                        count = len(eval(cases.split("=", 1)[1]))  # noqa: S307 - our own seed file
                        (tree / MEMBER).write_text(f"VALUE = 1\nHANDLED = {count}\n")
                    return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

            from foragent.git import GitRepo

            ctx = runner.RunContext("sandbox/repo", self.repo, GhTracker("sandbox/repo"),
                StateStore(self.state_root, now=lambda: self.clock), self.config,
                GitRepo(self.repo, self.git_transport), now=lambda: self.clock,
                executor_factory=Model,
                sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("Worker polled")),
                poll_hook=lambda execute, poll: execute())
            outcome = runner.run_issue(n, ctx)
            (self.root / f"worker-{n}-{len(os.listdir(self.root))}.json").write_text(
                json.dumps({"outcome": outcome}))

        process = multiprocessing.get_context("fork").Process(target=work)
        process.start()
        self.workers.append(process)
        process.join(120)
        assert not process.is_alive(), "Sandbox worker did not exit naturally; no signals are allowed"
        assert process.exitcode == 0, StateStore(self.state_root).log_path(n).read_text()
        return None

    def executor_turns(self):
        path = self.root / "executor-turns.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def outcomes(self):
        return [json.loads(p.read_text())["outcome"] for p in sorted(self.root.glob("worker-*.json"),
                key=lambda p: p.stat().st_mtime_ns)]

    def merged(self):
        return [row["subject"] for row in self.state()["merges"]]

    def drive(self, until, ticks=25, start_runs=True, step=0):
        """Tick the real scheduler (and merge owner) until ``until(world)`` holds.

        ``step`` moves the clock before each tick, in seconds: the gate's 300 s cadence lists
        GitHub again, so issues an order publication created become visible.
        """
        for _ in range(ticks):
            self.clock += timedelta(seconds=step)
            self.tick(start_runs=start_runs)
            if until(self):
                return True
        return False

    def fa(self, *argv, stdin=None):
        """Run one ``fa`` command in the sandbox checkout; returns (exit code, stdout)."""
        out = io.StringIO()
        previous = Path.cwd()
        old_stdin = sys.stdin
        os.chdir(self.repo)
        try:
            if stdin is not None:
                sys.stdin = io.StringIO(stdin)
            with redirect_stdout(out):
                try:
                    code = cli.main(list(argv))
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        finally:
            sys.stdin = old_stdin
            os.chdir(previous)
        return code or 0, out.getvalue()

    def store(self):
        return StateStore(self.state_root, now=lambda: self.clock)

    def pm_questions(self):
        """Every question ever recorded for the PM session or a person, open or answered."""
        return orch_inbox.questions(self.store(), include_closed=True)

    def labels(self, n=1):
        return {label["name"] for label in self.state()["issues"][str(n)]["labels"]}

    def person_routed(self):
        """Issues ever handed to a person: a needs-human label event or a needs-human marker comment."""
        state = self.state()
        labelled = {int(n) for n, events in state["events"].items()
                    if any(e.get("label", {}).get("name") == "needs-human" for e in events)}
        labelled |= {int(n) for n, issue in state["issues"].items()
                     if "needs-human" in {label["name"] for label in issue["labels"]}}
        marked = {int(n) for n, comments in state["comments"].items()
                  if any(c["body"].startswith("<!-- foragent:needs-human") for c in comments)}
        return labelled | marked

    def main_file(self, path):
        return git_command(self.origin, "show", f"main:{path}")

    def retry_marker(self, n=1):
        return retry_marker(self.state_root, n).exists()


def guard_network(monkeypatch, calls):
    """Refuse every outbound socket connection; local Unix sockets stay allowed."""
    real = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            calls.append(address)
            raise AssertionError(f"Network call to {address}")
        return real(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def guard_kicks(monkeypatch, kicks):
    """Record every detached tick kick instead of starting a real gate process."""
    from foragent import kick

    original = kick.kick_tick
    record = lambda repo_root: kicks.append(str(repo_root))  # noqa: E731
    monkeypatch.setattr(kick, "kick_tick", record)
    for name, module in list(sys.modules.items()):
        if name.startswith("foragent.") and getattr(module, "kick_tick", None) is original:
            monkeypatch.setattr(module, "kick_tick", record)


def guard_people(monkeypatch, pm_calls, person_calls):
    """Record any handoff to the PM session or a person instead of delivering it."""
    from foragent import notify

    for name in ("send", "deliver", "macos_notifier"):
        monkeypatch.setattr(notify, name, lambda *a, _n=name, **k: person_calls.append((_n, a, k)) or False)
    for name in ("ask", "ask_once", "answer"):
        original = getattr(orch_inbox, name)

        def recorded(*args, _name=name, _original=original, **kwargs):
            pm_calls.append((_name, args, kwargs))
            return _original(*args, **kwargs)

        monkeypatch.setattr(orch_inbox, name, recorded)


def main_adds_a_case(world):
    """Merged main adds a third case: the locked count of issue #1 goes stale."""
    world.main_update({CASES: "CASES = ['a', 'b', 'c']\n"}, "Add case c")
    return git_command(world.origin, "rev-parse", "main")


class FakeReader:
    """The preread model: a fake reader that finds no unknowns in a lock-only correction."""

    def __init__(self, name, turns):
        self.name, self.turns = name, turns

    def run(self, **kwargs):
        self.turns.append({"executor": self.name, "read_only": kwargs.get("read_only")})
        return ExecResult(0, False, "NONE", kwargs["log_path"])


# The only fa verbs the scripted orchestrator may use. Asking, answering or notifying is not one.
ORCH_VERBS = {("orch", "list"), ("orch", "questions"), ("orch", "inbox"), ("orch", "retry"),
              ("order", "preread"), ("order", "publish")}


class FakeOrchestrator:
    """A scripted orchestrator session. It acts only through fa commands, never asks the PM."""

    def __init__(self, world):
        self.world, self.commands = world, []
        self.folder = world.root / "orchestrator"
        self.folder.mkdir(exist_ok=True)

    def fa(self, *argv):
        assert tuple(argv[:2]) in ORCH_VERBS, argv
        self.commands.append(list(argv))
        return self.world.fa(*argv)

    def blocked(self):
        code, out = self.fa("orch", "list", "--json")
        assert code == 0, out
        return {row["number"]: row for row in json.loads(out)}

    def questions(self):
        code, out = self.fa("orch", "questions", "--json")
        assert code == 0, out
        return json.loads(out)

    def inbox(self):
        code, out = self.fa("orch", "inbox", "--json")
        assert code == 0, out
        return json.loads(out)

    def publish(self, plan):
        """Preread, then publish and approve the order itself (a choice-free fix)."""
        path = self.folder / f"{plan['id']}.json"
        path.write_text(json.dumps(plan, indent=2))
        code, out = self.fa("order", "preread", str(path))
        assert code == 0, out
        code, out = self.fa("order", "publish", str(path), "--approve")
        assert code == 0, out
        return out

    def correct_stale_lock(self, number, files):
        """A lock-only correction of an open, not running issue: the same goal, files and Done when."""
        row = self.blocked()[number]
        assert "running" not in self.world.labels(number), "The orchestrator edits no running issue"
        assert row["number"] == number
        plan = {"id": f"stale-lock-{number}", "title": f"Correct the stale locked test of #{number}",
                "summary": "Lock-only correction: merged main added a case; the test's purpose is unchanged.",
                "spec_markdown": "# Stale locked test\n\nMerged main added a third case. The locked test "
                                 "assumed two; its purpose (member 1 handles every case) is unchanged.\n",
                "issues": [],
                "edits": [{"number": number, **ISSUE_ONE, "done_when": [self.world.command],
                           "acceptance_files": files}]}
        return self.publish(plan)

    def resume(self, number):
        """Put the corrected issue back to work, if it still waits as blocked."""
        if number in self.blocked():
            code, out = self.fa("orch", "retry", str(number))
            assert code == 0, out


def new_issue_order(world):
    """An order with one new issue (member 7), as the orchestrator publishes a choice-free order."""
    test = ("from pathlib import Path\nimport runpy\n\n\n"
            "def test_member_seven():\n"
            "    assert runpy.run_path('src/member_7.py')['VALUE'] == 7\n")
    return {"id": "member-seven", "title": "Order: member seven", "summary": "Add member 7.",
            "spec_markdown": "# Member 7\n\nAdd member 7.\n",
            "issues": [{"key": "seven", "title": "Member 7", "lane": "main", "size": "s",
                        "goal": "Deliver member 7.", "out_of_scope": "Other members.",
                        "files": ["src/member_7.py"],
                        "done_when": [world.config.test_command + " tests/acceptance/test_member_7.py"],
                        "acceptance_files": {"tests/acceptance/test_member_7.py": test}}]}
