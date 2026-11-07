"""Simulation world for S6 (sim-no-lock-timeout): helpers, not tests.

The unattended-cycle world (tests/acceptance/unattended_cycle_world.py) with the real
scheduler, runner, publication check and merge owner, local Git, the executable fake
GitHub service and fake executors. This module adds only what S6 needs:

- merge_batch on, and two approved issues (#1 and #2) whose reviewed PRs form a real train;
- train CI that stays pending until the test releases it (the fake CI publishes no
  acceptance report for a train tip until then), so the train runs for a full cycle;
- network latency: while the overlap window is open, every push and fetch made by the
  gate process takes LATENCY seconds of real time, as a slow remote does;
- a preparation claim (``order_prepare_owner.claim``, the claim ``fa order prepare`` makes)
  started on its own thread at each of those slow network calls, so claims overlap the
  owner's train work and its publication landing;
- the lock-timeout events written anywhere in the sandbox.

FORAGENT_LOCK_TIMEOUT_SECONDS is set below LATENCY: a process that waits for a foragent
lock held across one slow network call gives up and records a lock-timeout event. No real
network, model, person or signal is used.
"""

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

from conftest import git_command
from test_merge_owner_e2e_sandbox import FAKE_GH, Sandbox, service_file
from unattended_cycle_world import EXTRA_GH, World
from foragent import order_prepare_owner, order_publications
from foragent.ci_workflow import WORKFLOW, measurement_scope, read_exclusions
from foragent.git import GitRepo
from foragent.state import StateStore

LATENCY = 1.0            # Seconds one push or fetch takes while the window is open.
LOCK_TIMEOUT = "0.4"     # FORAGENT_LOCK_TIMEOUT_SECONDS: a wait across one slow call gives up.
TRAIN_PREFIX = "foragent/batch-merge-"
PLAIN_OWN_1 = "from pathlib import Path\ndef test_own():\n    assert Path('src/member_1.py').exists()\n"

# The jobs of a workflow run (`actions/runs/<id>/jobs`), which the owner reads for a train
# tip whose run names its branch: a run the fake CI published has one finished, green
# acceptance job; an unknown run has none. The recent-runs list without a head SHA (the
# gate's CI-duration step) lists the published runs. Every other argv goes on to the
# shared fakes, unchanged.
JOBS_GH = r'''
import json as _j, os as _o, pathlib as _p, sys as _s
def _jobs():
    _a = _s.argv[1:]
    if _a[:1] != ['api']:
        return
    _e = next((a for a in _a[1:] if a.startswith('repos/')), '')
    _r = _e.split('?')[0].removeprefix('repos/sandbox/repo').strip('/').split('/')
    _st = _j.loads((_p.Path(_o.environ['FA_SANDBOX']).resolve() / 'github.json').read_text())
    if _r == ['actions', 'runs'] and 'head_sha=' not in _e:
        # The recent-runs list (CI durations): every run the fake CI published.
        import urllib.parse as _u
        _q = _u.parse_qs(_e.split('?', 1)[1] if '?' in _e else '')
        _runs = [r['run'] for r in _st['ci'].values()
                 if 'branch' not in _q or r['run'].get('head_branch') == _q['branch'][0]]
        print(_j.dumps(_runs if _q.get('page', ['1'])[0] == '1' else []))
        _s.exit(0)
    if len(_r) != 4 or _r[:2] != ['actions', 'runs'] or _r[3] != 'jobs':
        return
    _rows = [r for r in _st['ci'].values() if str(r['run']['id']) == _r[2]]
    print(_j.dumps([{'id': int(_r[2]) * 10 + 1, 'run_id': int(_r[2]), 'name': 'acceptance',
                     'status': 'completed', 'conclusion': 'success'} for _ in _rows]))
    _s.exit(0)
_jobs()
'''


def member_body(world, n):
    return ("## Goal\nDeliver member.\n## Files\n" + f"- src/member_{n}.py\n"
            "## Done when\n```sh\n" + world.config.test_command
            + f" tests/acceptance/test_own_{n}.py\n```\n")


def publication_order(world, n):
    """A choice-free order with one new issue (member ``n``), as the orchestrator publishes it."""
    test_path = f"tests/acceptance/test_member_{n}.py"
    test = ("import runpy\n\n\n"
            f"def test_member_{n}():\n"
            f"    assert runpy.run_path('src/member_{n}.py')['VALUE'] == {n}\n")
    return {"id": f"member-{n}", "title": f"Order: member {n}", "summary": f"Add member {n}.",
            "spec_markdown": f"# Member {n}\n\nAdd member {n}.\n",
            "issues": [{"key": f"m{n}", "title": f"Member {n}", "lane": "main", "size": "s",
                        "goal": f"Deliver member {n}.", "out_of_scope": "Other members.",
                        "files": [f"src/member_{n}.py"],
                        "done_when": [world.config.test_command + f" {test_path}"],
                        "acceptance_files": {test_path: test}}]}


class OverlapWorld(World):
    """Two approved issues that become one train, slow network and overlapping claims."""

    gate_pid = None
    window = False
    hold_train_ci = True

    def __init__(self, root, monkeypatch):
        monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", LOCK_TIMEOUT)
        super().__init__(root, monkeypatch, lock=PLAIN_OWN_1, seed_issue=True)
        gh = self.root / "bin" / "gh"
        gh.write_text(f"#!{sys.executable}\n" + JOBS_GH + EXTRA_GH + FAKE_GH)
        gh.chmod(0o755)
        self.gate_pid = os.getpid()
        self.window = False          # Latency and claims happen only while this is True.
        self.hold_train_ci = True    # Train tips get no acceptance report until released.
        self.held = []               # (root, head, base) of train pushes whose CI is pending.
        self.train_heads = []        # Every train tip pushed to the fake remote.
        self.claims = []             # One row per preparation claim attempt.
        self.claim_threads = []
        self.network_moments = []    # (verb, destination) of each slow call.
        settings = json.loads(self.main_file(".foragent.yml"))
        settings.update(merge_batch=True, merge_batch_size=4)
        self.main_update({".foragent.yml": json.dumps(settings)}, "Batch merge on")
        git_command(self.repo, "pull", "-q", "--ff-only", "origin", "main")
        from foragent.config import load_config

        self.config = load_config(self.repo)
        assert self.config.merge_owner and self.config.merge_batch
        with service_file(self.root) as state:
            # Issue #2 is approved too: with #1 it makes a train of two.
            state["issues"]["2"] = {"number": 2, "id": 2, "title": "Member 2", "body": member_body(self, 2),
                                    "state": "open", "labels": [{"name": "approved"}]}
            state["events"]["2"] = [{"label": {"name": "approved"}, "event": "labeled",
                                     "actor": {"login": "human"}, "created_at": self.clock.isoformat()}]
        # Member 2 is not on main yet: the seed put it there for the S1 scenario.
        git_command(self.repo, "pull", "-q", "--ff-only", "origin", "main")
        if "src/member_2.py" in git_command(self.origin, "ls-tree", "-r", "--name-only", "main").splitlines():
            stream = ("commit refs/heads/main\ncommitter Sandbox <test@example.com> 1792483200 +0000\n"
                      "data 15\nRemove member 2\n"
                      f"from {git_command(self.origin, 'rev-parse', 'main')}\nD src/member_2.py\n\ndone\n")
            git_command(self.origin, "fast-import", "--quiet", input=stream)
            git_command(self.repo, "pull", "-q", "--ff-only", "origin", "main")

    # --- workers: the merge-owner sandbox's model writes src/member_<n>.py ------------------
    def launch_worker(self, n):
        return Sandbox.launch_worker(self, n)

    # --- the fake remote --------------------------------------------------------------------
    def git_transport(self, argv):
        args = list(argv)
        while args[:1] == ["-C"]:
            args = args[2:]
        verb = args[0] if args else ""
        destination = ""
        if verb == "push":
            destination = args[-1].split(":", 1)[-1].removeprefix("refs/heads/")
        if verb in {"push", "fetch"}:
            self._slow(verb, destination)
        if verb == "push" and destination.startswith(TRAIN_PREFIX):
            return self._push_train(argv, destination)
        return super().git_transport(argv)

    def _push_train(self, argv, branch):
        root, args = self.repo, list(argv)
        while args[:1] == ["-C"]:
            root, args = Path(args[1]), args[2:]
        source = args[-1].split(":", 1)[0] if ":" in args[-1] else args[-1]
        for arg in args:
            if arg.startswith("--force-with-lease="):
                assert (GitRepo(self.repo).remote_branch_sha(branch) or "") == arg.rsplit(":", 1)[1]
        if not source:  # `push origin :refs/heads/<train>` deletes the train ref.
            git_command(self.origin, "update-ref", "-d", f"refs/heads/{branch}")
            return ""
        git_command(self.origin, "fetch", str(root), f"+{source}:refs/heads/{branch}")
        head = git_command(root, "rev-parse", source)
        base = git_command(self.origin, "rev-parse", "main")
        self.train_heads.append(head)
        if self.hold_train_ci:
            self.held.append((root, head, base, branch))
        else:
            self._publish_ci(root, head, base, branch)
        return ""

    def _publish_ci(self, root, head, base, branch):
        excluded = read_exclusions(GitRepo(root, self.git_transport), base) or []
        base_suite, head_suite = self.measure(base, excluded), self.measure(head, excluded)
        with service_file(self.root) as state:
            if head in state["ci"]:
                return
            run_id = 1000 + len(state["ci"])
            state["ci"][head] = {
                "run": {"id": run_id, "head_sha": head, "head_branch": branch, "status": "completed", "conclusion": "success",
                        "event": "pull_request", "path": WORKFLOW, "repository": {"full_name": "sandbox/repo"},
                        "head_repository": {"full_name": "sandbox/repo"}},
                "artifact": {"id": run_id * 10, "name": "acceptance-0", "expired": False,
                             "workflow_run": {"id": run_id, "head_sha": head}},
                "report": {"schema_version": 1, "repository": "sandbox/repo", "base_sha": base,
                           "head_sha": head, "excluded": excluded, "shard_count": 1, "shard_index": 0,
                           "duration_seconds": 0.01, "scope": measurement_scope(self.config, self.repo, excluded),
                           "base": base_suite, "head": head_suite}}

    def release_train_ci(self):
        """The train's CI finishes green: its acceptance report appears."""
        self.hold_train_ci = False
        held, self.held = self.held, []
        for root, head, base, branch in held:
            self._publish_ci(root, head, base, branch)

    # --- latency and overlapping claims -----------------------------------------------------
    def _slow(self, verb, destination):
        if not self.window or os.getpid() != self.gate_pid:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        self.network_moments.append((verb, destination))
        self.start_claim(f"during {verb} {destination}".strip())
        time.sleep(LATENCY)

    def start_claim(self, moment):
        """A preparation claim on its own thread, as a concurrent `fa order prepare` makes it."""
        index = len(self.claims)
        row = {"moment": moment, "train_running": self.train_running(), "result": None,
               "error": None, "seconds": None}
        self.claims.append(row)
        folder = self.state_root / "prepare"
        record = {"plan": f"overlap-claim-{index}", "issues": {}}
        digest = hashlib.sha256(moment.encode()).hexdigest()[:8]

        def claim():
            started = time.monotonic()
            try:
                row["result"] = order_prepare_owner.claim(folder, record, [f"issue-{index}-{digest}"],
                                                          lambda: None)
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                row["error"] = repr(exc)
            finally:
                row["seconds"] = time.monotonic() - started

        thread = threading.Thread(target=claim, daemon=True)
        thread.start()
        self.claim_threads.append(thread)

    def join_claims(self, seconds=30):
        for thread in self.claim_threads:
            thread.join(seconds)
        return [row for row in self.claims if row["seconds"] is None]

    # --- observations ------------------------------------------------------------------------
    def train_refs(self):
        listed = git_command(self.origin, "for-each-ref", "--format=%(refname)", f"refs/heads/{TRAIN_PREFIX}*")
        return [line for line in listed.splitlines() if line.strip()]

    def train_running(self):
        return bool(self.train_refs())

    def publications(self):
        return order_publications.requests(StateStore(self.state_root, now=lambda: self.clock))

    def created(self, title):
        return [issue for issue in self.state()["issues"].values() if issue["title"] == title]

    def lock_timeouts(self):
        events = []
        for base in {self.state_root, self.root}:
            for path in Path(base).rglob("orch-events.jsonl"):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() and json.loads(line).get("kind") == "lock-timeout":
                        events.append(json.loads(line))
        unique = {json.dumps(event, sort_keys=True): event for event in events}
        return list(unique.values())

    def step(self, seconds=300, *, start_runs=False):
        """One gate tick after ``seconds`` on the gate's clock (the 300 s cadence re-lists GitHub)."""
        self.clock += timedelta(seconds=seconds)
        return self.tick(start_runs=start_runs)
